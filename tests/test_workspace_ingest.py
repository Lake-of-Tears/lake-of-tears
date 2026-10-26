from __future__ import annotations

import os
import unittest.mock as mock
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pandas as pd
from starlette.testclient import TestClient

# Import pipeline and backend modules
import pipeline.shared.storage_writer as storage_writer
from ui.main import _WORKSPACE_COOKIE, AUTH_SECRET_KEY, _get_bucket_stats, app

# ── 1. StorageWriter & write_to_storage Tests ─────────────────────────────────


def test_write_to_storage_workspace_resolution():
    """Verify write_to_storage workspace resolution precedence and path construction."""
    df = pd.DataFrame([{"id": 1, "val": "alpha"}])

    # Case 1: Explicit workspace parameter
    with mock.patch("duckdb.connect") as mock_duckdb:
        mock_con = mock.MagicMock()
        mock_duckdb.return_value = mock_con

        path = storage_writer.write_to_storage(
            df,
            source="weather",
            date_str="2026-10-24",
            workspace="dev",
        )
        assert (
            path
            == "s3://datalake/workspaces/dev/raw/weather/year=2026/month=10/day=24/data.parquet"
        )
        mock_con.execute.assert_called_with(f"COPY df TO '{path}' (FORMAT PARQUET)")

    # Case 2: WORKSPACE_SLUG environment variable
    with mock.patch.dict(os.environ, {"WORKSPACE_SLUG": "finance", "WORKSPACE": "ignored"}):
        with mock.patch("duckdb.connect") as mock_duckdb:
            mock_con = mock.MagicMock()
            mock_duckdb.return_value = mock_con

            path = storage_writer.write_to_storage(
                df,
                source="stripe",
                date_str="2026-10-24",
            )
            assert (
                path
                == "s3://datalake/workspaces/finance/raw/stripe/year=2026/month=10/day=24/data.parquet"
            )

    # Case 3: WORKSPACE environment variable fallback
    with mock.patch.dict(os.environ, {"WORKSPACE": "marketing"}, clear=True):
        with mock.patch("duckdb.connect") as mock_duckdb:
            mock_con = mock.MagicMock()
            mock_duckdb.return_value = mock_con

            path = storage_writer.write_to_storage(
                df,
                source="shopify",
                date_str="2026-10-24",
            )
            assert (
                path
                == "s3://datalake/workspaces/marketing/raw/shopify/year=2026/month=10/day=24/data.parquet"
            )

    # Case 4: Default fallback
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("duckdb.connect") as mock_duckdb:
            mock_con = mock.MagicMock()
            mock_duckdb.return_value = mock_con

            path = storage_writer.write_to_storage(
                df,
                source="truenas",
                date_str="2026-10-24",
            )
            assert (
                path
                == "s3://datalake/workspaces/default/raw/truenas/year=2026/month=10/day=24/data.parquet"
            )


def test_write_to_storage_catalog_workspace_scoping():
    """Verify catalog, schema, table usage is jailed under the active workspace."""
    df = pd.DataFrame([{"id": 100, "amount": 42.5}])

    with mock.patch("duckdb.connect") as mock_duckdb:
        mock_con = mock.MagicMock()
        mock_duckdb.return_value = mock_con

        path = storage_writer.write_to_storage(
            df,
            date_str="2026-10-24",
            workspace="analytics",
            catalog="production",
            schema="revenue",
            table="stripe_charges",
        )
        assert (
            path
            == "s3://datalake/workspaces/analytics/production/revenue/stripe_charges/year=2026/month=10/day=24/data.parquet"
        )


def test_storage_writer_helper_class():
    """Verify StorageWriter helper class exposes write with workspace support."""
    df = pd.DataFrame([{"k": "v"}])

    with mock.patch("duckdb.connect") as mock_duckdb:
        mock_con = mock.MagicMock()
        mock_duckdb.return_value = mock_con

        path = storage_writer.StorageWriter.write(
            df,
            source="jellyfin",
            date_str="2026-10-24",
            workspace="media",
        )
        assert (
            path
            == "s3://datalake/workspaces/media/raw/jellyfin/year=2026/month=10/day=24/data.parquet"
        )


# ── 2. Cross-Workspace Isolation Guarantee ─────────────────────────────────────


def test_workspace_dev_never_affects_workspace_prod_storage():
    """Acceptance Criteria: Ingestion jobs in Workspace Dev never write to or affect Workspace Prod storage."""
    df_dev = pd.DataFrame([{"event": "dev_click"}])
    df_prod = pd.DataFrame([{"event": "prod_sale"}])

    executed_queries = []

    def mock_execute(query):
        executed_queries.append(query)

    with mock.patch("duckdb.connect") as mock_duckdb:
        mock_con = mock.MagicMock()
        mock_con.execute.side_effect = mock_execute
        mock_duckdb.return_value = mock_con

        # Run dev ingest
        path_dev = storage_writer.write_to_storage(
            df_dev, source="weather", date_str="2026-10-24", workspace="dev"
        )
        # Run prod ingest
        path_prod = storage_writer.write_to_storage(
            df_prod, source="weather", date_str="2026-10-24", workspace="prod"
        )

        assert "workspaces/dev/raw/weather" in path_dev
        assert "workspaces/prod/raw/weather" in path_prod
        assert path_dev != path_prod
        assert not path_dev.startswith("s3://datalake/workspaces/prod/")
        assert not path_prod.startswith("s3://datalake/workspaces/dev/")


# ── 3. Workspace-Scoped Bucket Summary & Status Metrics ────────────────────────


def test_get_bucket_stats_isolated_to_active_workspace():
    """Acceptance Criteria: Ingestion summary page reports metrics solely for the active workspace."""
    # Mock S3 objects: Dev has weather data, Prod has truenas data, Root has legacy data
    now = datetime.now(UTC)
    mock_objects = [
        {
            "Key": "workspaces/dev/raw/postgres/year=2026/month=10/day=24/data.parquet",
            "Size": 1024,
            "LastModified": now,
        },
        {
            "Key": "workspaces/prod/raw/stripe/year=2026/month=10/day=24/data.parquet",
            "Size": 2048,
            "LastModified": now,
        },
        {
            "Key": "raw/postgres/year=2026/month=10/day=24/data.parquet",
            "Size": 4096,
            "LastModified": now,
        },
    ]

    mock_s3 = mock.MagicMock()
    mock_paginator = mock.MagicMock()
    mock_s3.get_paginator.return_value = mock_paginator

    def paginate(**kwargs):
        prefix = kwargs.get("Prefix", "")
        matching = [o for o in mock_objects if o["Key"].startswith(prefix)]
        return [{"Contents": matching}]

    mock_paginator.paginate.side_effect = paginate

    with mock.patch("ui.main._s3_client", return_value=mock_s3):
        # Stats for dev workspace
        dev_stats = _get_bucket_stats(workspace_slug="dev")
        assert dev_stats["workspace"] == "dev"
        assert dev_stats["total_objects"] == 1
        assert dev_stats["total_size"] == "1.0 KB"
        # Find postgres source in dev
        postgres_dev = next(s for s in dev_stats["sources"] if s["key"] == "postgres")
        assert postgres_dev["status"] == "green"
        stripe_dev = next(s for s in dev_stats["sources"] if s["key"] == "stripe")
        assert stripe_dev["status"] == "red"  # No data in dev

        # Stats for prod workspace
        prod_stats = _get_bucket_stats(workspace_slug="prod")
        assert prod_stats["workspace"] == "prod"
        assert prod_stats["total_objects"] == 1
        assert prod_stats["total_size"] == "2.0 KB"
        postgres_prod = next(s for s in prod_stats["sources"] if s["key"] == "postgres")
        assert postgres_prod["status"] == "red"  # No data in prod
        stripe_prod = next(s for s in prod_stats["sources"] if s["key"] == "stripe")
        assert stripe_prod["status"] == "green"


# ── 4. UI Ingestion Route & Template Rendering ─────────────────────────────────


def test_ui_ingest_page_workspace_context():
    """Verify /ingest renders with active workspace badge and jailed S3 paths."""
    client = TestClient(app)

    token = jwt.encode(
        {"sub": "admin@lake.local", "email": "admin@lake.local", "role": "admin"},
        AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    mock_ws = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": "admin"}]

    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    mock_objects = [
        {
            "Key": "workspaces/analytics/raw/postgres/year=2026/month=10/day=24/data.parquet",
            "Size": 1024,
            "LastModified": now,
        },
    ]

    mock_s3 = mock.MagicMock()
    mock_paginator = mock.MagicMock()
    mock_s3.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [{"Contents": mock_objects}]

    with (
        mock.patch("ui.main._s3_client", return_value=mock_s3),
        mock.patch("ui.main._fetch_workspaces", return_value=mock_ws),
    ):
        resp = client.get(
            "/ingest",
            cookies={"lake_token": token, _WORKSPACE_COOKIE: "ws-1"},
        )
        assert resp.status_code == 200
        html = resp.text

        # Check workspace badge
        assert "Workspace: analytics" in html
        # Check S3 jailed path rendered in table
        assert "workspaces/analytics/raw/postgres/" in html


# ── 5. Airflow System DAGs Verification ─────────────────────────────────────────


def test_airflow_system_dags_presence():
    """Verify core platform DAGs exist in pipeline/dags without legacy hardcoded feeds."""
    dags_dir = Path(__file__).parents[1] / "pipeline" / "dags"
    assert (dags_dir / "run_notebook_dag.py").exists()
    assert (dags_dir / "refresh_catalog_schemas_dag.py").exists()
    assert (dags_dir / "embed_sources_dag.py").exists()

    # Legacy hardcoded homelab DAGs should not exist
    for legacy in [
        "ingest_weather_dag.py",
        "ingest_truenas_dag.py",
        "ingest_truenas_logs_dag.py",
        "ingest_jellyfin_dag.py",
        "ingest_tmdb_movies_dag.py",
    ]:
        assert not (dags_dir / legacy).exists()
