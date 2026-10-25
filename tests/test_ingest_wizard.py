from __future__ import annotations

import io
import json
import os
import unittest.mock as mock
from datetime import UTC, date, datetime

import duckdb
import jwt
import pandas as pd
import pytest
from starlette.testclient import TestClient

from ui import ingest_service
from pipeline.shared.toon import parse_toon, dump_toon
from pipeline.shared.connector_registry import registry as connector_registry
from ui.main import _WORKSPACE_COOKIE, AUTH_SECRET_KEY, app

# ── 1. Unit Tests: Ingest Service & TOON Logic ────────────────────────────────


def test_toon_parsing_and_dumping():
    sample_toon = """id: custom_feed
name: Custom Webhook Feed
version: 1.0.0
category: Streaming & Events
author: Developer
description: Real-time webhook stream
active: true
tags: [webhook, json, streaming]
parameters[2]{name,type,description}:
  secret,string,Webhook signing secret
  batch_size,int,Batch buffer size
"""
    parsed = parse_toon(sample_toon)
    assert parsed["id"] == "custom_feed"
    assert parsed["name"] == "Custom Webhook Feed"
    assert parsed["active"] is True
    assert parsed["tags"] == ["webhook", "json", "streaming"]
    assert len(parsed["parameters"]) == 2
    assert parsed["parameters"][0]["name"] == "secret"
    assert parsed["parameters"][0]["type"] == "string"

    dumped = dump_toon(parsed)
    assert "id: custom_feed" in dumped
    assert "parameters[2]{name,type,description}:" in dumped


def test_connector_registry_discovery():
    connector_registry.reload()
    connectors = connector_registry.list_connectors()
    assert len(connectors) >= 10
    ids = [c["id"] for c in connectors]
    assert "postgres" in ids
    assert "stripe" in ids
    assert "mysql" in ids
    assert "kafka" in ids
    assert "snowflake" in ids
    assert "shopify" in ids
    assert "hubspot" in ids

    categories = connector_registry.get_categories()
    assert "Databases & Warehouses" in categories
    assert "Finance & SaaS" in categories
    assert "E-Commerce" in categories
    assert "Streaming & Events" in categories


def test_slugify():
    assert ingest_service.slugify("My Ingested Data.CSV") == "my_ingested_data"
    assert ingest_service.slugify("Sales_Report-2026.parquet") == "sales_report_2026"
    assert ingest_service.slugify("Special #*! Characters") == "special_characters"
    assert ingest_service.slugify("") == "table"


def test_detect_file_format():
    assert ingest_service.detect_file_format("data.csv") == "csv"
    assert ingest_service.detect_file_format("data.tsv") == "tsv"
    assert ingest_service.detect_file_format("records.json") == "json"
    assert ingest_service.detect_file_format("events.ndjson") == "ndjson"
    assert ingest_service.detect_file_format("events.jsonl") == "ndjson"
    assert ingest_service.detect_file_format("table.parquet") == "parquet"
    assert ingest_service.detect_file_format("table.pq") == "parquet"

    with pytest.raises(ValueError, match="Unsupported file format"):
        ingest_service.detect_file_format("unsupported.xml")


def test_preview_uploaded_csv():
    csv_bytes = b"id,name,amount\n1,Alice,42.5\n2,Bob,100.0\n3,Charlie,15.25\n"
    res = ingest_service.preview_uploaded_file(csv_bytes, "customers.csv")
    assert res["ok"] is True
    assert res["format"] == "csv"
    assert res["suggested_table_name"] == "customers"
    assert len(res["columns"]) == 3
    col_names = [c["name"] for c in res["columns"]]
    assert "id" in col_names
    assert "name" in col_names
    assert "amount" in col_names
    assert len(res["rows"]) == 3
    assert res["rows"][0][1] == "Alice"


def test_preview_uploaded_json():
    json_bytes = b'[{"user": "u1", "score": 98}, {"user": "u2", "score": 85}]'
    res = ingest_service.preview_uploaded_file(json_bytes, "scores.json")
    assert res["ok"] is True
    assert res["format"] == "json"
    assert res["suggested_table_name"] == "scores"
    col_names = [c["name"] for c in res["columns"]]
    assert "user" in col_names
    assert "score" in col_names
    assert len(res["rows"]) == 2


def test_preview_uploaded_parquet():
    df = pd.DataFrame([{"metric": "cpu", "value": 78.4}, {"metric": "mem", "value": 52.1}])
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    pq_bytes = buf.getvalue()

    res = ingest_service.preview_uploaded_file(pq_bytes, "metrics.parquet")
    assert res["ok"] is True
    assert res["format"] == "parquet"
    assert res["suggested_table_name"] == "metrics"
    col_names = [c["name"] for c in res["columns"]]
    assert "metric" in col_names
    assert "value" in col_names
    assert len(res["rows"]) == 2


def test_convert_to_parquet_file():
    csv_bytes = b"dept,budget\nfinance,500000\nengineering,1200000\n"
    pq_path, columns, row_count = ingest_service.convert_to_parquet_file(csv_bytes, "budgets.csv")
    assert os.path.exists(pq_path)
    try:
        assert row_count == 2
        col_names = [c["name"] for c in columns]
        assert "dept" in col_names
        assert "budget" in col_names
        # Verify file is valid Parquet readable by DuckDB
        con = duckdb.connect()
        loaded = con.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
        assert loaded == 2
    finally:
        if os.path.exists(pq_path):
            os.remove(pq_path)


def test_build_managed_hive_path():
    fixed_date = date(2026, 10, 25)
    key, pattern = ingest_service.build_managed_hive_path(
        workspace_slug="analytics",
        catalog_slug="production",
        schema_slug="bronze",
        table_slug="customer_events",
        target_date=fixed_date,
        mode="create_new",
    )
    assert key == "workspaces/analytics/production/bronze/customer_events/year=2026/month=10/day=25/data.parquet"
    assert pattern == "s3://datalake/workspaces/analytics/production/bronze/customer_events/**/*.parquet"

    # Append mode should append a unique suffix to the data file
    key_append, pattern_append = ingest_service.build_managed_hive_path(
        workspace_slug="analytics",
        catalog_slug="production",
        schema_slug="bronze",
        table_slug="customer_events",
        target_date=fixed_date,
        mode="append",
    )
    assert key_append.startswith("workspaces/analytics/production/bronze/customer_events/year=2026/month=10/day=25/data_")
    assert key_append.endswith(".parquet")
    assert pattern_append == pattern


def test_validate_and_jail_s3_path():
    # Relative workspace path
    assert (
        ingest_service.validate_and_jail_s3_path("raw/weather/", "dev")
        == "s3://datalake/workspaces/dev/raw/weather/**/*.parquet"
    )
    # Explicit workspace path
    assert (
        ingest_service.validate_and_jail_s3_path("workspaces/dev/raw/weather/data.parquet", "dev")
        == "s3://datalake/workspaces/dev/raw/weather/data.parquet"
    )
    # With s3://datalake prefix
    assert (
        ingest_service.validate_and_jail_s3_path("s3://datalake/workspaces/dev/raw/weather/", "dev")
        == "s3://datalake/workspaces/dev/raw/weather/**/*.parquet"
    )

    # Cross-workspace rejection
    with pytest.raises(ValueError, match="Cross-workspace path access denied"):
        ingest_service.validate_and_jail_s3_path("workspaces/finance/raw/weather/", "dev")

    # Directory traversal rejection
    with pytest.raises(ValueError, match="Directory traversal"):
        ingest_service.validate_and_jail_s3_path("../etc/passwd", "dev")


# ── 2. Endpoint Tests: Add Data Hub APIs ─────────────────────────────────────────


@pytest.fixture
def test_tokens():
    admin_token = jwt.encode(
        {"sub": "admin@lake.local", "email": "admin@lake.local", "role": "admin"},
        AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    read_token = jwt.encode(
        {"sub": "viewer@lake.local", "email": "viewer@lake.local", "role": "user"},
        AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    return {"admin": admin_token, "read": read_token}


def test_api_preview_file_endpoint(test_tokens):
    client = TestClient(app)
    csv_content = b"id,name,value\n10,ItemA,99.9\n20,ItemB,149.5\n"

    resp = client.post(
        "/api/ingest/preview-file",
        files={"file": ("items.csv", csv_content, "text/csv")},
        cookies={"lake_token": test_tokens["admin"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["filename"] == "items.csv"
    assert data["suggested_table_name"] == "items"
    assert len(data["columns"]) == 3
    assert len(data["rows"]) == 2


def test_api_create_table_from_file_endpoint(test_tokens):
    client = TestClient(app)
    csv_content = b"user_id,action\nu123,login\nu456,purchase\n"

    mock_catalogs = [
        {
            "id": "cat-uuid-1",
            "name": "Main Catalog",
            "slug": "main",
            "schemas": [
                {
                    "id": "sch-uuid-1",
                    "name": "bronze",
                    "slug": "bronze",
                    "tables": [],
                }
            ],
        }
    ]

    mock_s3 = mock.MagicMock()
    mock_backend_post = mock.MagicMock(return_value={"id": "tbl-uuid-1", "name": "user_actions"})

    with (
        mock.patch("ui.main._resolve_ui_catalogs", return_value=mock_catalogs),
        mock.patch("ui.main._s3_client", return_value=mock_s3),
        mock.patch("ui.main._backend_post", mock_backend_post),
    ):
        resp = client.post(
            "/api/ingest/create-table-from-file",
            data={
                "catalog_id": "cat-uuid-1",
                "schema_id": "sch-uuid-1",
                "table_name": "user_actions",
                "mode": "create_new",
                "description": "User actions test table",
            },
            files={"file": ("user_actions.csv", csv_content, "text/csv")},
            cookies={
                "lake_token": test_tokens["admin"],
                _WORKSPACE_COOKIE: "dev",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["table_name"] == "user_actions"
        assert data["schema_slug"] == "bronze"
        assert data["sql_view"] == "bronze.user_actions"
        assert "s3://datalake/workspaces/default/main/bronze/user_actions/**/*.parquet" in data["s3_path_pattern"]

        # Verify S3 upload occurred
        mock_s3.upload_file.assert_called_once()
        # Verify backend table registration payload included inferred column_defs
        mock_backend_post.assert_called_once()
        post_args, post_kwargs = mock_backend_post.call_args
        registered_table = post_args[2]
        assert registered_table["name"] == "user_actions"
        assert len(registered_table["column_defs"]) == 2


def test_api_create_table_duplicate_rejection(test_tokens):
    client = TestClient(app)
    csv_content = b"id\n1\n"

    mock_catalogs = [
        {
            "id": "cat-uuid-1",
            "name": "Main",
            "slug": "main",
            "schemas": [
                {
                    "id": "sch-uuid-1",
                    "name": "bronze",
                    "slug": "bronze",
                    "tables": [{"id": "t1", "name": "existing_table", "slug": "existing_table"}],
                }
            ],
        }
    ]

    with mock.patch("ui.main._resolve_ui_catalogs", return_value=mock_catalogs):
        resp = client.post(
            "/api/ingest/create-table-from-file",
            data={
                "catalog_id": "cat-uuid-1",
                "schema_id": "sch-uuid-1",
                "table_name": "existing_table",
                "mode": "create_new",
            },
            files={"file": ("existing_table.csv", csv_content, "text/csv")},
            cookies={"lake_token": test_tokens["admin"]},
        )
        assert resp.status_code == 409
        assert "already exists in schema" in resp.json()["error"]


def test_api_create_table_read_only_rejection(test_tokens):
    client = TestClient(app)
    csv_content = b"id\n1\n"

    # Workspace where user only holds 'read' role
    mock_ws = [{"id": "ws-1", "name": "Dev", "slug": "dev", "my_role": "read", "role": "read"}]

    with mock.patch("ui.main._fetch_workspaces", return_value=mock_ws):
        resp = client.post(
            "/api/ingest/create-table-from-file",
            data={
                "catalog_id": "cat-1",
                "schema_id": "sch-1",
                "table_name": "test_table",
            },
            files={"file": ("test.csv", csv_content, "text/csv")},
            cookies={
                "lake_token": test_tokens["read"],
                _WORKSPACE_COOKIE: "ws-1",
            },
        )
        assert resp.status_code == 403
        assert "Write or Admin" in resp.json()["error"]


def test_api_register_s3_path_endpoint(test_tokens):
    client = TestClient(app)

    mock_catalogs = [
        {
            "id": "cat-uuid-1",
            "name": "Main",
            "slug": "main",
            "schemas": [{"id": "sch-uuid-1", "name": "silver", "slug": "silver"}],
        }
    ]

    mock_columns = [{"name": "station_id", "type": "VARCHAR"}, {"name": "temp", "type": "FLOAT"}]
    mock_backend_post = mock.MagicMock(return_value={"id": "tbl-uuid-2", "name": "weather_history"})

    mock_ws = [{"id": "ws-dev", "name": "Dev", "slug": "dev", "my_role": "admin"}]

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_ws),
        mock.patch("ui.main._resolve_ui_catalogs", return_value=mock_catalogs),
        mock.patch("ui.ingest_service.inspect_s3_parquet_path", return_value=mock_columns),
        mock.patch("ui.main._backend_post", mock_backend_post),
    ):
        resp = client.post(
            "/api/ingest/register-s3-path",
            json={
                "s3_path": "workspaces/dev/raw/weather/",
                "catalog_id": "cat-uuid-1",
                "schema_id": "sch-uuid-1",
                "table_name": "weather_history",
            },
            cookies={
                "lake_token": test_tokens["admin"],
                _WORKSPACE_COOKIE: "ws-dev",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["table_name"] == "weather_history"
        assert data["sql_view"] == "silver.weather_history"
        assert data["columns"] == mock_columns


def test_api_connectors_endpoint(test_tokens):
    client = TestClient(app)

    mock_stats = {
        "ok": True,
        "sources": [
            {
                "key": "postgres",
                "label": "PostgreSQL",
                "status": "green",
                "last_modified_str": "2026-10-25 14:00 UTC",
            },
            {
                "key": "stripe",
                "label": "Stripe",
                "status": "green",
                "last_modified_str": "2026-10-25 14:15 UTC",
            },
            {
                "key": "kafka",
                "label": "Apache Kafka",
                "status": "red",
                "last_modified_str": "Never",
            },
        ],
    }

    with mock.patch("ui.main._get_bucket_stats", return_value=mock_stats):
        resp = client.get(
            "/api/ingest/connectors",
            cookies={"lake_token": test_tokens["admin"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        connectors = data["connectors"]
        connector_keys = [c["key"] for c in connectors]
        assert "postgres" in connector_keys
        assert "stripe" in connector_keys
        assert "kafka" in connector_keys
        pg_conn = next(c for c in connectors if c["key"] == "postgres")
        assert pg_conn["dag_id"] == "ingest_postgres"
        assert pg_conn["status"] == "green"


def test_ui_ingest_page_layout_and_navigation(test_tokens):
    """Verify /ingest renders with Databricks-style sub-tabs, Add Data title, and navigation shortcuts."""
    client = TestClient(app)

    mock_ws = [{"id": "ws-1", "name": "Dev", "slug": "dev", "my_role": "admin"}]
    mock_stats = {"ok": True, "sources": []}
    mock_catalogs = [{"id": "c1", "name": "Default Catalog", "slug": "default", "schemas": []}]

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_ws),
        mock.patch("ui.main._get_bucket_stats", return_value=mock_stats),
        mock.patch("ui.main._resolve_ui_catalogs", return_value=mock_catalogs),
    ):
        resp = client.get(
            "/ingest",
            cookies={"lake_token": test_tokens["admin"], _WORKSPACE_COOKIE: "ws-1"},
        )
        assert resp.status_code == 200
        html = resp.text

        # Verify Title & Sub-tabs
        assert "Add Data" in html
        assert "Add New Data" in html
        assert "Active Feeds & Connectors" in html

        # Verify Databricks Cards
        assert "Create or Modify Table (File Upload)" in html
        assert "Register Existing S3 Path to Table" in html

        # Verify Sidebar Link Renamed
        assert ">Add Data</a>" in html or "Add Data\n      </a>" in html
