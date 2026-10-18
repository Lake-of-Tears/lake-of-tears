from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from models import Base, Catalog, CatalogSchema, Workspace  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_catalog_order", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend_main)


def test_schema_sort_key_medallion_tiers():
    class DummySchema:
        def __init__(self, tier, slug, name):
            self.tier = tier
            self.slug = slug
            self.name = name

    schemas = [
        DummySchema("gold", "gold", "Gold"),
        DummySchema(None, "custom", "Analytics"),
        DummySchema("bronze", "bronze", "Bronze"),
        DummySchema("silver", "silver", "Silver"),
    ]
    sorted_s = sorted(schemas, key=backend_main._schema_sort_key)
    assert [s.slug for s in sorted_s] == ["bronze", "silver", "gold", "custom"]


def test_catalog_schemas_relationship_order_by():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws_id = uuid.uuid4()
    ws = Workspace(id=ws_id, name="Default WS", slug="default-ws")
    session.add(ws)
    session.commit()

    cat_id = uuid.uuid4()
    cat = Catalog(
        id=cat_id,
        name="Test Catalog",
        slug="test-catalog",
        owner_workspace_id=ws_id,
    )
    session.add(cat)
    session.commit()

    # Add schemas in intentionally non-medallion / alphabetical order
    session.add_all(
        [
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Gold", slug="gold", tier="gold"
            ),
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Silver", slug="silver", tier="silver"
            ),
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Bronze", slug="bronze", tier="bronze"
            ),
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Reporting", slug="reporting", tier=None
            ),
        ]
    )
    session.commit()

    # Query catalog and verify schemas relationship order
    fresh_cat = session.query(Catalog).filter_by(id=cat_id).first()
    names = [s.name for s in fresh_cat.schemas]
    assert names == ["Bronze", "Silver", "Gold", "Reporting"]


def test_catalog_response_and_list_schemas_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws_id = uuid.uuid4()
    ws = Workspace(id=ws_id, name="Default WS", slug="default-ws")
    session.add(ws)
    session.commit()

    cat_id = uuid.uuid4()
    cat = Catalog(
        id=cat_id,
        name="Test Medallion",
        slug="test-medallion",
        owner_workspace_id=ws_id,
    )
    session.add(cat)
    session.commit()

    session.add_all(
        [
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Gold", slug="gold", tier="gold"
            ),
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Silver", slug="silver", tier="silver"
            ),
            CatalogSchema(
                id=uuid.uuid4(), catalog_id=cat_id, name="Bronze", slug="bronze", tier="bronze"
            ),
        ]
    )
    session.commit()

    fresh_cat = session.query(Catalog).filter_by(id=cat_id).first()
    user = MagicMock()
    user.role = "superadmin"
    user.workspace_memberships = []

    resp = backend_main._catalog_response(fresh_cat, user, session)
    assert [s.slug for s in resp.schemas] == ["bronze", "silver", "gold"]

    schemas_resp = backend_main.list_schemas(cat_id, session, user)
    assert [s.slug for s in schemas_resp] == ["bronze", "silver", "gold"]
