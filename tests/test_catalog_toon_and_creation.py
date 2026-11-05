from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

import ui.main as ui_main
from pipeline.shared.toon_catalog import (
    schema_to_toon,
    table_to_toon,
    toon_to_sql_ddl,
    toon_to_table_spec,
)


@pytest.fixture
def ui_client():
    return TestClient(ui_main.app)


def _make_token(sub="00000000-0000-0000-0000-000000000001", role="admin"):
    return jwt.encode(
        {"sub": sub, "email": "admin@lake.local", "role": role},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )


# ── TOON Catalog Serializer & Parser Tests ────────────────────────────────────


def test_table_to_toon_serialization():
    table = {
        "slug": "customer_orders",
        "name": "Customer Orders",
        "description": "Daily customer orders and fulfillment items",
        "s3_path_pattern": "s3://datalake/workspaces/default/main/silver/customer_orders/*.parquet",
        "column_defs": [
            {
                "name": "order_id",
                "type": "VARCHAR",
                "description": "Unique order ID",
                "nullable": False,
            },
            {
                "name": "customer_id",
                "type": "VARCHAR",
                "description": "Customer foreign key",
                "nullable": False,
            },
            {
                "name": "line_items",
                "type": "JSON",
                "description": "Array of item objects",
                "nullable": True,
            },
            {
                "name": "total_amount",
                "type": "DOUBLE",
                "description": "Total order charge",
                "nullable": True,
            },
            {
                "name": "status",
                "type": "VARCHAR",
                "description": "Order lifecycle status",
                "nullable": True,
            },
            {
                "name": "created_at",
                "type": "TIMESTAMP",
                "description": "Order timestamp",
                "nullable": True,
            },
        ],
    }
    toon_str = table_to_toon(table, catalog_name="main", schema_name="silver")
    assert "table: customer_orders" in toon_str
    assert "catalog: main" in toon_str
    assert "schema: silver" in toon_str
    assert "columns[6]{name,type,description,nullable}:" in toon_str
    assert "order_id,VARCHAR,Unique order ID,false" in toon_str
    assert "total_amount,DOUBLE,Total order charge,true" in toon_str


def test_schema_to_toon_serialization():
    schema = {
        "slug": "silver",
        "name": "Silver Curated",
        "tier": "silver",
        "description": "Cleaned, standardized analytical datasets",
        "tables": [
            {"slug": "orders", "description": "Customer orders", "column_defs": [1, 2, 3]},
            {"slug": "customers", "description": "Verified accounts", "column_defs": [1, 2, 3, 4]},
        ],
    }
    toon_str = schema_to_toon(schema, catalog_name="main")
    assert "schema: silver" in toon_str
    assert "catalog: main" in toon_str
    assert "tier: silver" in toon_str
    assert "tables[2]{name,columns,description}:" in toon_str
    assert "orders,3,Customer orders" in toon_str
    assert "customers,4,Verified accounts" in toon_str


def test_toon_to_table_spec_and_ddl():
    sample_toon = """
table: user_events
name: User Events
catalog: telemetry
schema: bronze
description: Raw telemetry clickstream events
columns[4]{name,type,description,nullable}:
  event_id,VARCHAR,Primary event key,false
  user_id,VARCHAR,User identifier,false
  event_type,VARCHAR,Click or impression,true
  event_timestamp,TIMESTAMP,Event timestamp,true
"""
    spec = toon_to_table_spec(sample_toon)
    assert spec["table"] == "user_events"
    assert spec["catalog"] == "telemetry"
    assert spec["schema"] == "bronze"
    assert len(spec["columns"]) == 4
    assert spec["columns"][0]["name"] == "event_id"
    assert spec["columns"][0]["nullable"] is False

    ddl = toon_to_sql_ddl(spec)
    assert 'CREATE TABLE "bronze"."user_events"' in ddl
    assert '"event_id" VARCHAR NOT NULL' in ddl
    assert '"event_timestamp" TIMESTAMP' in ddl


# ── AI Service Heuristic & Generation Tests ───────────────────────────────────


def test_ai_generate_table_from_prompt_heuristic():
    import backend.ai_service as ai_service

    prompt = (
        "Create a customer orders table with order_id, user_id, amount, line_items json, and status"
    )
    result = ai_service.generate_table_from_prompt(prompt, catalog="main", schema="silver")
    assert result["ok"] is True
    assert "orders" in result["table_name"]
    assert "columns" in result
    col_names = [c["name"] for c in result["columns"]]
    assert any("order_id" in name or "id" in name for name in col_names)
    assert result["toon"]
    assert result["sql_ddl"]


def test_ai_generate_semantic_description_heuristic():
    import backend.ai_service as ai_service

    result = ai_service.generate_semantic_description(
        asset_type="schema",
        name="finance_reporting",
        catalog_name="main",
    )
    assert result["ok"] is True
    assert (
        "Finance Reporting" in result["description"] or "finance" in result["description"].lower()
    )

    table_result = ai_service.generate_semantic_description(
        asset_type="table",
        name="monthly_invoices",
        columns=[
            {"name": "invoice_id", "type": "VARCHAR"},
            {"name": "billed_amount", "type": "DOUBLE"},
            {"name": "created_at", "type": "TIMESTAMP"},
        ],
    )
    assert table_result["ok"] is True
    assert "invoice_id" in table_result["column_descriptions"]
    assert "billed_amount" in table_result["column_descriptions"]


# ── UI Route Tests: Catalog Schema and Table Creation via TOON / File ─────────


def test_ui_api_create_schema(ui_client):
    token = _make_token()
    cat_id = str(uuid.uuid4())

    with patch("ui.main._backend_post") as mock_post:
        mock_post.return_value = {
            "id": str(uuid.uuid4()),
            "catalog_id": cat_id,
            "name": "analytics",
            "slug": "analytics",
            "description": "Analytics domain schema",
        }

        resp = ui_client.post(
            f"/api/catalog/{cat_id}/schemas",
            json={"name": "analytics", "description": "Analytics domain schema"},
            cookies={"lake_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["schema"]["slug"] == "analytics"


def test_ui_api_prompt_to_table(ui_client):
    token = _make_token()

    with patch("ui.main._backend_post") as mock_post:
        mock_post.return_value = {
            "ok": True,
            "toon": "table: telemetry\ncolumns[1]{name,type,description,nullable}:\n  id,VARCHAR,id,false",
            "table_name": "telemetry",
            "display_name": "Telemetry",
            "description": "Telemetry data",
            "columns": [{"name": "id", "type": "VARCHAR", "description": "id", "nullable": False}],
            "sql_ddl": 'CREATE TABLE "bronze"."telemetry" ("id" VARCHAR NOT NULL);',
        }

        resp = ui_client.post(
            "/api/catalog/ai/prompt-to-table",
            json={"prompt": "Create telemetry table", "catalog": "main", "schema": "bronze"},
            cookies={"lake_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["table_name"] == "telemetry"


def test_ui_api_create_table_from_toon(ui_client):
    token = _make_token()
    cat_id = str(uuid.uuid4())
    sch_id = str(uuid.uuid4())
    tbl_id = str(uuid.uuid4())

    mock_catalogs = [
        {
            "id": cat_id,
            "slug": "main",
            "name": "Main Catalog",
            "schemas": [{"id": sch_id, "slug": "bronze", "name": "Bronze Layer"}],
        }
    ]

    mock_s3 = MagicMock()
    with (
        patch("ui.main._resolve_ui_catalogs", return_value=mock_catalogs),
        patch("ui.main._s3_client", return_value=mock_s3),
        patch("ui.main._backend_post") as mock_post,
    ):
        mock_post.return_value = {
            "id": tbl_id,
            "name": "user_clicks",
            "slug": "user_clicks",
            "column_defs": [{"name": "click_id", "type": "VARCHAR"}],
        }

        resp = ui_client.post(
            "/api/catalog/create-table-from-toon",
            data={
                "catalog_id": cat_id,
                "schema_id": sch_id,
                "table_name": "user_clicks",
                "description": "User clickstream telemetry",
                "columns_json": json.dumps(
                    [
                        {
                            "name": "click_id",
                            "type": "VARCHAR",
                            "description": "Primary key",
                            "nullable": False,
                        },
                        {
                            "name": "page_url",
                            "type": "VARCHAR",
                            "description": "Target URL",
                            "nullable": True,
                        },
                    ]
                ),
            },
            cookies={"lake_token": token},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["table_id"] == tbl_id
        assert data["table_name"] == "user_clicks"
        assert mock_s3.upload_file.called


def test_ui_api_export_toon_routes(ui_client):
    token = _make_token()
    cat_id = str(uuid.uuid4())
    sch_id = str(uuid.uuid4())
    tbl_id = str(uuid.uuid4())

    with patch("ui.main._backend_get") as mock_get:
        mock_get.return_value = {
            "ok": True,
            "toon": "schema: silver\ntables[1]{name,columns,description}:\n  orders,5,Orders table",
            "schema": "silver",
            "filename": "silver.toon",
        }

        resp = ui_client.get(
            f"/api/catalog/{cat_id}/schemas/{sch_id}/toon",
            cookies={"lake_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "schema: silver" in data["toon"]

    with patch("ui.main._backend_get") as mock_get:
        mock_get.return_value = {
            "ok": True,
            "toon": "table: orders\ncolumns[2]{name,type,description,nullable}:\n  id,VARCHAR,id,false",
            "table": "orders",
            "filename": "orders.toon",
        }

        resp = ui_client.get(
            f"/api/catalog/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/toon",
            cookies={"lake_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "table: orders" in data["toon"]


def test_backend_export_toon_functions():
    backend_dir = str(Path(__file__).parents[1] / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    from models import Catalog, CatalogSchema, CatalogTable, User

    import backend.main as b_main

    db = MagicMock()
    cat_id = str(uuid.uuid4())
    sch_id = str(uuid.uuid4())
    tbl_id = str(uuid.uuid4())

    mock_table = CatalogTable(
        id=uuid.UUID(tbl_id),
        name="Orders",
        slug="orders",
        description="Orders data",
        s3_path_pattern="s3://datalake/orders/*.parquet",
        column_defs=[{"name": "id", "type": "VARCHAR", "nullable": False}],
    )
    mock_schema = CatalogSchema(
        id=uuid.UUID(sch_id),
        catalog_id=uuid.UUID(cat_id),
        name="Silver",
        slug="silver",
        tier="silver",
        description="Silver schema",
    )
    mock_schema.tables = [mock_table]
    mock_table.schema = mock_schema

    mock_cat = Catalog(
        id=uuid.UUID(cat_id),
        name="Main",
        slug="main",
    )
    mock_cat.schemas = [mock_schema]

    def _query_side_effect(model):
        m = MagicMock()
        if model == Catalog:
            m.filter.return_value.first.return_value = mock_cat
        elif model == CatalogSchema:
            m.filter_by.return_value.first.return_value = mock_schema
        elif model == CatalogTable:
            m.filter_by.return_value.first.return_value = mock_table
        return m

    db.query.side_effect = _query_side_effect
    user = User(id=uuid.uuid4(), email="admin@lake.local", role="admin")

    with patch.object(b_main, "_assert_catalog_access"):
        # Test schema export JSON
        sch_resp = b_main.export_schema_toon(
            catalog_id=cat_id,
            schema_id=sch_id,
            format=None,
            db=db,
            current_user=user,
            ws_ctx=None,
        )
        assert sch_resp["ok"] is True
        assert "schema: silver" in sch_resp["toon"]
        assert "orders" in sch_resp["toon"]

        # Test schema export RAW plain text
        raw_sch = b_main.export_schema_toon(
            catalog_id=cat_id,
            schema_id=sch_id,
            format="raw",
            db=db,
            current_user=user,
            ws_ctx=None,
        )
        assert raw_sch.status_code == 200
        assert "schema: silver" in raw_sch.body.decode()

        # Test table export JSON
        tbl_resp = b_main.export_table_toon(
            catalog_id=cat_id,
            schema_id=sch_id,
            table_id=tbl_id,
            format=None,
            db=db,
            current_user=user,
            ws_ctx=None,
        )
        assert tbl_resp["ok"] is True
        assert "table: orders" in tbl_resp["toon"]
        assert "id,VARCHAR" in tbl_resp["toon"]

        # Test table export RAW plain text
        raw_tbl = b_main.export_table_toon(
            catalog_id=cat_id,
            schema_id=sch_id,
            table_id=tbl_id,
            format="raw",
            db=db,
            current_user=user,
            ws_ctx=None,
        )
        assert raw_tbl.status_code == 200
        assert "table: orders" in raw_tbl.body.decode()
