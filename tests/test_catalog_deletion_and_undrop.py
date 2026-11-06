"""Unit and integration tests for Catalog Schema & Table Deletion, CASCADE, and 7-day UNDROP recovery (#236)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# Setup import paths
backend_dir = str(Path(__file__).parents[1] / "backend")
ui_dir = str(Path(__file__).parents[1] / "ui")
shared_dir = str(Path(__file__).parents[1] / "pipeline" / "shared")
for d in (backend_dir, ui_dir, shared_dir):
    if d not in sys.path:
        sys.path.insert(0, d)

import catalog_trash_service  # noqa: E402
import pytest  # noqa: E402
import sql_guardrails  # noqa: E402
from catalog_storage import is_pattern_in_workspace, s3_prefix_for_pattern  # noqa: E402
from database import Base  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from models import (  # noqa: E402
    Catalog,
    CatalogSchema,
    CatalogTable,
    DeletedCatalogAsset,
    PipelineMetadata,
    User,
    Workspace,
)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture
def db_session(tmp_path):
    """In-memory SQLite session with all models created."""
    db_file = tmp_path / "test_catalog_deletion.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    yield session

    session.close()


@pytest.fixture
def mock_catalog_fixture(db_session):
    """Create test workspace, user, catalog, schemas, and tables."""
    user = User(
        id=uuid.uuid4(),
        email="operator@lakehouse.test",
        display_name="Lake Operator",
        role="superadmin",
        is_active=True,
    )
    db_session.add(user)

    ws = Workspace(
        id=uuid.uuid4(),
        name="Finance Workspace",
        slug="finance",
        created_by=user.id,
    )
    db_session.add(ws)

    cat = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=ws.id,
        name="finance_lake",
        slug="finance_lake",
    )
    db_session.add(cat)

    # Locked medallion schema
    bronze_sch = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat.id,
        name="bronze",
        slug="bronze",
        tier="bronze",
    )
    db_session.add(bronze_sch)

    # Custom analytics schema
    custom_sch = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat.id,
        name="analytics",
        slug="analytics",
        tier="custom",
    )
    db_session.add(custom_sch)

    # Managed table in analytics schema
    managed_table = CatalogTable(
        id=uuid.uuid4(),
        schema_id=custom_sch.id,
        name="monthly_revenue",
        slug="monthly_revenue",
        description="Monthly aggregated revenue",
        s3_path_pattern=f"s3://datalake/workspaces/{ws.slug}/finance_lake/analytics/monthly_revenue/data.parquet",
        column_defs=[
            {"name": "month", "type": "VARCHAR", "nullable": False},
            {"name": "amount", "type": "DOUBLE", "nullable": True},
        ],
        vector_index={
            "status": "synced",
            "dest_s3": f"s3://datalake/workspaces/{ws.slug}/embeddings/monthly_revenue/data.parquet",
        },
    )
    db_session.add(managed_table)

    # External table in analytics schema
    external_table = CatalogTable(
        id=uuid.uuid4(),
        schema_id=custom_sch.id,
        name="raw_partner_events",
        slug="raw_partner_events",
        description="External partner events",
        s3_path_pattern="s3://datalake/external-partners/events/*.parquet",
        column_defs=[{"name": "event_id", "type": "BIGINT", "nullable": False}],
    )
    db_session.add(external_table)

    db_session.commit()

    return {
        "user": user,
        "workspace": ws,
        "catalog": cat,
        "bronze_schema": bronze_sch,
        "analytics_schema": custom_sch,
        "managed_table": managed_table,
        "external_table": external_table,
    }


def test_managed_vs_external_table_pattern():
    """Verify managed table detection logic."""
    assert is_pattern_in_workspace(
        "s3://datalake/workspaces/finance/cat/sch/tbl/data.parquet", "finance"
    )
    assert not is_pattern_in_workspace(
        "s3://datalake/external-partners/events/*.parquet", "finance"
    )
    assert not is_pattern_in_workspace("s3://datalake/workspaces/marketing/cat/sch/tbl/", "finance")

    assert (
        s3_prefix_for_pattern("s3://datalake/workspaces/finance/cat/sch/tbl/data.parquet")
        == "workspaces/finance/cat/sch/tbl/"
    )
    assert (
        s3_prefix_for_pattern("s3://datalake/workspaces/finance/cat/sch/tbl/*.parquet")
        == "workspaces/finance/cat/sch/tbl/"
    )


def test_medallion_schema_deletion_blocked(db_session, mock_catalog_fixture):
    """Ensure medallion schemas (bronze, silver, gold) cannot be deleted."""
    f = mock_catalog_fixture
    with pytest.raises(HTTPException) as exc_info:
        catalog_trash_service.soft_delete_schema(
            db=db_session,
            schema=f["bronze_schema"],
            catalog=f["catalog"],
            workspace=f["workspace"],
            user_id=f["user"].id,
            cascade=True,
        )
    assert exc_info.value.status_code == 400
    assert "locked 'bronze' medallion schema" in exc_info.value.detail


def test_schema_delete_restrict_when_child_tables_exist(db_session, mock_catalog_fixture):
    """Ensure non-empty schema deletion fails with 409 Conflict if cascade=False."""
    f = mock_catalog_fixture
    with pytest.raises(HTTPException) as exc_info:
        catalog_trash_service.soft_delete_schema(
            db=db_session,
            schema=f["analytics_schema"],
            catalog=f["catalog"],
            workspace=f["workspace"],
            user_id=f["user"].id,
            cascade=False,
        )
    assert exc_info.value.status_code == 409
    assert "contains 2 table(s). Use CASCADE" in exc_info.value.detail


@patch("catalog_trash_service.move_prefix", return_value=1)
def test_schema_delete_cascade_and_batch_tracking(mock_move, db_session, mock_catalog_fixture):
    """Verify cascade deletion soft-deletes child tables and creates batch records."""
    f = mock_catalog_fixture
    sch_id = f["analytics_schema"].id

    deleted_assets = catalog_trash_service.soft_delete_schema(
        db=db_session,
        schema=f["analytics_schema"],
        catalog=f["catalog"],
        workspace=f["workspace"],
        user_id=f["user"].id,
        cascade=True,
    )
    db_session.commit()

    # 2 tables + 1 schema = 3 deleted assets
    assert len(deleted_assets) == 3
    batch_ids = {a.batch_id for a in deleted_assets}
    assert len(batch_ids) == 1
    assert None not in batch_ids

    # Verify active schema and tables were removed
    assert db_session.query(CatalogSchema).filter_by(id=sch_id).first() is None
    assert db_session.query(CatalogTable).filter_by(schema_id=sch_id).count() == 0

    # Verify items are tracked in deleted_catalog_assets
    trash_items = catalog_trash_service.list_workspace_trash(db_session, f["workspace"].id)
    assert len(trash_items) == 3


@patch("catalog_trash_service.move_prefix", return_value=1)
def test_managed_table_soft_delete_and_undrop(mock_move, db_session, mock_catalog_fixture):
    """Verify managed table soft delete moves storage to trash and UNDROP restores it."""
    f = mock_catalog_fixture
    tbl = f["managed_table"]
    tbl_name = tbl.name
    tbl_slug = tbl.slug
    sch = f["analytics_schema"]

    # 1. Soft-delete the table
    del_asset = catalog_trash_service.soft_delete_table(
        db=db_session,
        table=tbl,
        schema=sch,
        catalog=f["catalog"],
        workspace=f["workspace"],
        user_id=f["user"].id,
    )
    db_session.commit()

    assert del_asset.is_managed is True
    assert f".trash/{tbl.id}/" in (del_asset.trash_s3_prefix or "")
    assert f".trash/{tbl.id}_embeddings/" in (del_asset.trash_embeddings_prefix or "")
    assert db_session.query(CatalogTable).filter_by(slug=tbl_slug).first() is None

    # 2. Verify it shows in workspace trash
    trash = catalog_trash_service.list_workspace_trash(db_session, f["workspace"].id)
    matching = [t for t in trash if t.slug == tbl_slug]
    assert len(matching) == 1
    assert matching[0].days_remaining > 0

    # 3. UNDROP restore
    res = catalog_trash_service.restore_asset(db_session, del_asset.id, f["workspace"])
    assert res["ok"] is True

    # 4. Table is restored in active catalog
    restored = db_session.query(CatalogTable).filter_by(slug=tbl_slug).first()
    assert restored is not None
    assert restored.name == tbl_name
    assert len(restored.column_defs) == 2

    # 5. Asset is removed from trash
    assert db_session.query(DeletedCatalogAsset).filter_by(id=del_asset.id).first() is None


@patch("catalog_trash_service.move_prefix", return_value=1)
def test_undrop_collision_handling(mock_move, db_session, mock_catalog_fixture):
    """Verify UNDROP fails with 409 Conflict if a table with the same name was recreated."""
    f = mock_catalog_fixture
    tbl = f["managed_table"]
    sch = f["analytics_schema"]

    # Soft delete
    del_asset = catalog_trash_service.soft_delete_table(
        db=db_session,
        table=tbl,
        schema=sch,
        catalog=f["catalog"],
        workspace=f["workspace"],
        user_id=f["user"].id,
    )
    db_session.commit()

    # Recreate table with same slug
    new_tbl = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch.id,
        name=del_asset.name,
        slug=del_asset.slug,
        description="Recreated table",
    )
    db_session.add(new_tbl)
    db_session.commit()

    # Attempting to UNDROP original table must raise 409 Conflict
    with pytest.raises(HTTPException) as exc_info:
        catalog_trash_service.restore_asset(db_session, del_asset.id, f["workspace"])
    assert exc_info.value.status_code == 409
    assert "already exists in schema" in exc_info.value.detail


@patch("catalog_trash_service.delete_prefix", return_value=1)
def test_purge_expired_assets_ttl(mock_del, db_session, mock_catalog_fixture):
    """Verify 7-day TTL sweeper purges expired trash assets."""
    f = mock_catalog_fixture
    now = datetime.now(UTC)

    # 1. Asset expired 1 day ago
    expired_asset = DeletedCatalogAsset(
        id=uuid.uuid4(),
        workspace_id=f["workspace"].id,
        catalog_id=f["catalog"].id,
        asset_type="table",
        name="old_logs",
        slug="old_logs",
        trash_s3_prefix="workspaces/finance/.trash/old_logs/",
        dropped_at=now - timedelta(days=8),
        purge_at=now - timedelta(days=1),
    )
    db_session.add(expired_asset)

    # 2. Asset dropped yesterday, expires in 6 days
    active_trash_asset = DeletedCatalogAsset(
        id=uuid.uuid4(),
        workspace_id=f["workspace"].id,
        catalog_id=f["catalog"].id,
        asset_type="table",
        name="recent_drop",
        slug="recent_drop",
        trash_s3_prefix="workspaces/finance/.trash/recent_drop/",
        dropped_at=now - timedelta(days=1),
        purge_at=now + timedelta(days=6),
    )
    db_session.add(active_trash_asset)
    db_session.commit()

    # Run TTL purge sweeper
    purged_count = catalog_trash_service.purge_expired_assets(db_session)
    assert purged_count == 1

    # Verify expired was deleted, active remains
    assert db_session.query(DeletedCatalogAsset).filter_by(id=expired_asset.id).first() is None
    assert (
        db_session.query(DeletedCatalogAsset).filter_by(id=active_trash_asset.id).first()
        is not None
    )


def test_sql_guardrails_undrop():
    """Verify is_write_sql identifies UNDROP as a write/DDL operation."""
    assert sql_guardrails.is_write_sql("UNDROP TABLE monthly_revenue")
    assert sql_guardrails.is_write_sql("undrop schema analytics")
    assert sql_guardrails.is_write_sql("  UNDROP TABLE finance_lake.analytics.monthly_revenue;  ")
    assert not sql_guardrails.is_write_sql("SELECT * FROM monthly_revenue")


@patch("catalog_trash_service.move_prefix", return_value=1)
def test_sql_interceptor_undrop_statement(mock_move, db_session, mock_catalog_fixture):
    """Verify SQL statement runner intercepts UNDROP TABLE and restores asset."""
    f = mock_catalog_fixture
    tbl = f["managed_table"]
    sch = f["analytics_schema"]

    # Soft delete table first
    catalog_trash_service.soft_delete_table(
        db=db_session,
        table=tbl,
        schema=sch,
        catalog=f["catalog"],
        workspace=f["workspace"],
        user_id=f["user"].id,
    )
    db_session.commit()

    # Run UNDROP TABLE statement
    handled, msg = catalog_trash_service.try_handle_undrop_statement(
        sql="UNDROP TABLE monthly_revenue",
        workspace=f["workspace"],
        user=f["user"],
        default_catalog=f["catalog"].slug,
        default_schema=sch.slug,
        db=db_session,
    )

    assert handled is True
    assert "Restored table 'monthly_revenue'" in (msg or "")

    # Table is back in database
    restored = db_session.query(CatalogTable).filter_by(slug="monthly_revenue").first()
    assert restored is not None


def test_dependency_checking(tmp_path, db_session, mock_catalog_fixture):
    """Verify DAG file scanning and PipelineMetadata dependency checks."""
    f = mock_catalog_fixture
    ws = f["workspace"]

    # Add pipeline metadata referencing the table
    pipeline_meta = PipelineMetadata(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        dag_id=f"ws_{ws.slug}_revenue_pipeline",
        display_label="Daily Revenue ETL",
        description="Extracts data and updates monthly_revenue table",
    )
    db_session.add(pipeline_meta)
    db_session.commit()

    # Mock Airflow DAGs directory with a python DAG mentioning the table
    dags_dir = tmp_path / "airflow_dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    sample_dag = dags_dir / f"ws_{ws.slug}_ingest.py"
    sample_dag.write_text("""
from airflow import DAG
# Process finance analytics
target = "monthly_revenue"
""")

    with patch("catalog_trash_service.get_airflow_dags_dir", return_value=dags_dir):
        deps = catalog_trash_service.check_table_dependencies(
            table=f["managed_table"],
            schema=f["analytics_schema"],
            catalog=f["catalog"],
            workspace=ws,
            db=db_session,
        )

    assert deps.has_dependencies is True
    assert len(deps.dependencies) >= 2
    types = {d.type for d in deps.dependencies}
    assert "dag" in types
    assert "pipeline" in types
