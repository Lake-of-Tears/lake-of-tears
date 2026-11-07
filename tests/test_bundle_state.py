"""Unit and integration tests for Lake Asset Bundles state tracking and drift detection service (#422)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


# Handle SQLite JSONB compilation in unit test environment
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import pytest  # noqa: E402
from database import Base  # noqa: E402
from models import (  # noqa: E402
    Catalog,
    CatalogSchema,
    CatalogTable,
    PipelineMetadata,
    User,
    Workspace,
    WorkspaceBundle,
    WorkspaceDashboard,
)

from backend.bundles import BundleSpec, BundleStateService, parse_bundle_str  # noqa: E402


@pytest.fixture
def db_session(tmp_path):
    """Temporary SQLite database session with all models registered."""
    db_file = tmp_path / "test_bundles_state.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def test_workspace(db_session: Session) -> Workspace:
    """Create a persistent test workspace."""
    ws = Workspace(
        id=uuid.uuid4(),
        name="Analytics Platform",
        slug=f"analytics-{uuid.uuid4().hex[:6]}",
        status="active",
        created_at=datetime.now(UTC),
    )
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


@pytest.fixture
def test_user(db_session: Session) -> User:
    """Create a test user."""
    user = User(
        id=uuid.uuid4(),
        email=f"dataops-{uuid.uuid4().hex[:6]}@lakeoftears.io",
        display_name="DataOps Engineer",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sample_spec() -> BundleSpec:
    """Return a validated sample BundleSpec."""
    manifest = """
    bundle:
      name: customer-lakehouse
      version: 1.0.0
      description: Production Customer 360 Lakehouse Bundle
    variables:
      bucket:
        default: datalake
    targets:
      dev:
        workspace: dev-ws
      prod:
        workspace: prod-ws
    resources:
      catalogs:
        main_cat:
          description: Main Analytics Catalog
    """
    return parse_bundle_str(manifest, format="yaml")


def test_register_new_bundle(
    db_session: Session, test_workspace: Workspace, test_user: User, sample_spec: BundleSpec
):
    """Test registering a new WorkspaceBundle record."""
    resources_map = {
        "catalogs": [str(uuid.uuid4())],
        "schemas": [str(uuid.uuid4())],
        "tables": [str(uuid.uuid4())],
    }

    bundle = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
        deployed_by=test_user.id,
        git_commit_sha="a1b2c3d4",
        resources_map=resources_map,
        status="deployed",
    )
    db_session.commit()

    assert bundle.id is not None
    assert bundle.workspace_id == test_workspace.id
    assert bundle.bundle_name == "customer-lakehouse"
    assert bundle.target == "dev"
    assert bundle.version == "1.0.0"
    assert bundle.git_commit_sha == "a1b2c3d4"
    assert bundle.deployed_by == test_user.id
    assert bundle.status == "deployed"
    assert bundle.resources_map == resources_map
    assert bundle.manifest_snapshot["bundle"]["name"] == "customer-lakehouse"


def test_update_existing_bundle(
    db_session: Session, test_workspace: Workspace, test_user: User, sample_spec: BundleSpec
):
    """Test updating an existing bundle deployment with a new version and commit."""
    # 1. Initial registration
    bundle1 = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
        deployed_by=test_user.id,
        git_commit_sha="v1-sha",
        resources_map={"catalogs": ["cat-1"]},
        status="deployed",
    )
    db_session.commit()
    initial_id = bundle1.id

    # 2. Update with version 1.1.0
    updated_manifest = """
    bundle:
      name: customer-lakehouse
      version: 1.1.0
      description: Updated version
    """
    spec_v2 = parse_bundle_str(updated_manifest, format="yaml")
    bundle2 = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=spec_v2,
        target="dev",
        deployed_by=test_user.id,
        git_commit_sha="v2-sha",
        resources_map={"catalogs": ["cat-1", "cat-2"]},
        status="deployed",
    )
    db_session.commit()

    assert bundle2.id == initial_id
    assert bundle2.version == "1.1.0"
    assert bundle2.git_commit_sha == "v2-sha"
    assert bundle2.resources_map == {"catalogs": ["cat-1", "cat-2"]}


def test_get_bundle_queries(
    db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec
):
    """Test querying bundles by (workspace, name, target) and by UUID."""
    created = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="staging",
    )
    db_session.commit()

    by_key = BundleStateService.get_bundle(
        db_session, test_workspace.id, "customer-lakehouse", target="staging"
    )
    assert by_key is not None
    assert by_key.id == created.id

    by_id = BundleStateService.get_bundle_by_id(db_session, created.id)
    assert by_id is not None
    assert by_id.bundle_name == "customer-lakehouse"

    # Non-existent query returns None
    missing = BundleStateService.get_bundle(db_session, test_workspace.id, "unknown", target="dev")
    assert missing is None


def test_list_bundles_with_filtering(
    db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec
):
    """Test listing bundles within a workspace and filtering by target."""
    # Register 'dev' target
    BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
    )
    # Register 'prod' target
    BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="prod",
    )
    db_session.commit()

    all_bundles = BundleStateService.list_bundles(db_session, test_workspace.id)
    assert len(all_bundles) == 2

    dev_bundles = BundleStateService.list_bundles(db_session, test_workspace.id, target="dev")
    assert len(dev_bundles) == 1
    assert dev_bundles[0].target == "dev"


def test_update_bundle_status(
    db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec
):
    """Test updating the lifecycle status of a bundle."""
    bundle = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
        status="deployed",
    )
    db_session.commit()

    updated = BundleStateService.update_bundle_status(db_session, bundle.id, "destroying")
    db_session.commit()
    assert updated.status == "destroying"

    with pytest.raises(ValueError, match="not found"):
        BundleStateService.update_bundle_status(db_session, uuid.uuid4(), "failed")


def test_delete_bundle(db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec):
    """Test deleting a bundle deployment record."""
    bundle = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
    )
    db_session.commit()

    deleted = BundleStateService.delete_bundle(db_session, bundle.id)
    db_session.commit()
    assert deleted is True

    # Assert no longer found
    assert BundleStateService.get_bundle_by_id(db_session, bundle.id) is None
    # Deleting non-existent returns False
    assert BundleStateService.delete_bundle(db_session, uuid.uuid4()) is False


def test_drift_detection_in_sync(
    db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec
):
    """Test drift detection when all deployed assets exist in the database."""
    # Create actual live catalog and schema
    cat = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=test_workspace.id,
        name="prod_catalog",
        slug="prod-catalog",
    )
    db_session.add(cat)
    db_session.flush()

    sch = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat.id,
        name="bronze",
        slug="bronze",
    )
    db_session.add(sch)
    db_session.flush()

    tbl = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch.id,
        name="events",
        slug="events",
    )
    db_session.add(tbl)
    db_session.flush()

    dash = WorkspaceDashboard(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        title="overview",
        slug="overview",
        layout_config=[],
    )
    db_session.add(dash)
    db_session.flush()

    pipe = PipelineMetadata(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        dag_id="daily_sync",
        display_label="Daily Sync",
    )
    db_session.add(pipe)
    db_session.commit()

    # Track all created assets in bundle
    resources_map = {
        "catalogs": [str(cat.id)],
        "schemas": [str(sch.id)],
        "tables": [str(tbl.id)],
        "dashboards": [str(dash.id)],
        "pipelines": [str(pipe.id)],
    }
    bundle = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
        resources_map=resources_map,
        status="deployed",
    )
    db_session.commit()

    report = BundleStateService.detect_drift(db_session, bundle)
    assert report["in_sync"] is True
    assert report["missing_count"] == 0
    assert report["current_status"] == "deployed"


def test_drift_detection_with_missing_assets(
    db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec
):
    """Test drift detection when tracked assets have been dropped outside of the bundle."""
    # Track non-existent UUIDs
    ghost_cat_id = str(uuid.uuid4())
    ghost_tbl_id = str(uuid.uuid4())
    ghost_dash_id = str(uuid.uuid4())

    resources_map = {
        "catalogs": [ghost_cat_id],
        "tables": [ghost_tbl_id],
        "dashboards": [ghost_dash_id],
    }

    bundle = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="prod",
        resources_map=resources_map,
        status="deployed",
    )
    db_session.commit()

    report = BundleStateService.detect_drift(db_session, bundle)
    assert report["in_sync"] is False
    assert report["missing_count"] == 3
    assert ghost_cat_id in report["missing_resources"]["catalogs"]
    assert ghost_tbl_id in report["missing_resources"]["tables"]
    assert ghost_dash_id in report["missing_resources"]["dashboards"]
    # Status should automatically transition to pending_drift_sync
    assert report["current_status"] == "pending_drift_sync"


def test_unique_constraint_target(db_session: Session, test_workspace: Workspace):
    """Test that duplicate (workspace_id, bundle_name, target) records are rejected by unique constraint."""
    b1 = WorkspaceBundle(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        bundle_name="unique-bundle",
        target="dev",
        version="1.0.0",
    )
    db_session.add(b1)
    db_session.commit()

    b2 = WorkspaceBundle(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        bundle_name="unique-bundle",
        target="dev",
        version="2.0.0",
    )
    db_session.add(b2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_workspace_cascade_deletion(
    db_session: Session, test_workspace: Workspace, sample_spec: BundleSpec
):
    """Test that deleting a workspace cascades and removes its bundle records."""
    bundle = BundleStateService.register_or_update_bundle(
        db=db_session,
        workspace_id=test_workspace.id,
        bundle_spec=sample_spec,
        target="dev",
    )
    db_session.commit()
    bundle_id = bundle.id

    # Delete parent workspace
    db_session.delete(test_workspace)
    db_session.commit()

    # Bundle record should be cascade deleted
    assert BundleStateService.get_bundle_by_id(db_session, bundle_id) is None
