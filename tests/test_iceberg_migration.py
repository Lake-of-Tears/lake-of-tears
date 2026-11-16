"""Tests for Phase 5: In-place Parquet-to-Iceberg zero-copy migration engine (#548)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import table_scan
from iceberg_testkit import _REPO_ROOT, client, iceberg_env  # noqa: F401
from models import CatalogTable


def _write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))


def test_zero_copy_migration_unpartitioned(env):
    """Zero-copy migration converts an unpartitioned Parquet table to Iceberg without moving data files."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    # Create raw Parquet files in the workspace storage directory
    table_dir = env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "orders_parquet"
    data1 = pa.table({"id": pa.array([1, 2], pa.int64()), "amount": [19.99, 45.50]})
    data2 = pa.table({"id": pa.array([3], pa.int64()), "amount": [99.00]})
    _write_parquet(table_dir / "part-0.parquet", data1)
    _write_parquet(table_dir / "part-1.parquet", data2)

    # Register as legacy Parquet table in LoT catalog
    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Orders Parquet",
        slug="orders_parquet",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/**/*.parquet",
        column_defs=[{"name": "id", "type": "BIGINT"}, {"name": "amount", "type": "DOUBLE"}],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    # 1. Dry run migration
    resp_dry = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": True},
    )
    assert resp_dry.status_code == 200
    dry_data = resp_dry.json()
    assert dry_data["dry_run"] is True
    assert dry_data["files"] == 2
    assert dry_data["rows"] == 3

    # Table is still parquet after dry-run
    db.refresh(tbl)
    assert tbl.format == "parquet"

    # 2. Real migration
    resp_mig = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp_mig.status_code == 200
    mig_data = resp_mig.json()
    assert mig_data["ok"] is True
    assert mig_data["files"] == 2

    # Verify table transitioned to Iceberg
    db.refresh(tbl)
    assert tbl.format == "iceberg"
    assert tbl.iceberg is not None
    assert tbl.iceberg.metadata_location.endswith(".metadata.json")
    assert tbl.iceberg.toon_summary is not None

    # Underlying Parquet data files stayed in their exact original paths (zero copy!)
    assert (table_dir / "part-0.parquet").exists()
    assert (table_dir / "part-1.parquet").exists()

    # Query through DuckDB iceberg_scan
    scan = table_scan.table_scan_for(tbl)
    assert scan.startswith("iceberg_scan(")
    con = duckdb.connect()
    assert table_scan.ensure_iceberg_loaded(con)
    res = con.execute(f"SELECT sum(amount) FROM {scan}").fetchone()[0]
    assert pytest.approx(res, 0.01) == 164.49
    db.close()


def test_zero_copy_migration_with_hive_partitions(env):
    """Zero-copy migration discovers Hive partitions (e.g. region=west) and preserves partition specs."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    # Create Hive partitioned directory structure
    table_dir = (
        env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "events_partitioned"
    )
    data_west = pa.table({"id": pa.array([1], pa.int64()), "event": ["login"]})
    data_east = pa.table({"id": pa.array([2], pa.int64()), "event": ["logout"]})
    _write_parquet(table_dir / "region=west" / "data.parquet", data_west)
    _write_parquet(table_dir / "region=east" / "data.parquet", data_east)

    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Events Partitioned",
        slug="events_partitioned",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/**/*.parquet",
        column_defs=[{"name": "id", "type": "BIGINT"}, {"name": "event", "type": "VARCHAR"}],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    resp = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp.status_code == 200

    # Read back through Iceberg catalog
    rest = env["rest"](env["writer"])
    ice_table = rest.load_table(("main_lake", "bronze", "events_partitioned"))
    assert ice_table.scan().to_arrow().num_rows == 2
    assert "region" in ice_table.schema().column_names
    assert ice_table.spec().fields[0].name == "region"
    db.close()


def test_zero_copy_migration_with_null_columns(env):
    """Zero-copy migration sanitizes untyped pa.null() columns using physical Parquet types."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    table_dir = (
        env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "invoices_with_nulls"
    )
    # PyArrow table with a null-typed column
    data = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "cleared_bank_ref": pa.array([None, None], pa.null()),
            "amount": pa.array([10.5, 20.0], pa.float64()),
        }
    )
    _write_parquet(table_dir / "data.parquet", data)

    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Invoices With Nulls",
        slug="invoices_with_nulls",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/data.parquet",
        column_defs=[
            {"name": "id", "type": "BIGINT"},
            {"name": "cleared_bank_ref", "type": "VARCHAR"},
            {"name": "amount", "type": "DOUBLE"},
        ],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    resp = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp.status_code == 200
    mig_data = resp.json()
    assert mig_data["ok"] is True

    db.refresh(tbl)
    assert tbl.format == "iceberg"
    assert tbl.iceberg is not None

    scan = table_scan.table_scan_for(tbl)
    con = duckdb.connect()
    assert table_scan.ensure_iceberg_loaded(con)
    res = con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0]
    assert res == 2
    db.close()


def test_zero_copy_migration_empty_table(env):
    """Zero-copy migration initializes empty Parquet catalog tables into valid Iceberg tables."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    table_dir = (
        env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "empty_catalog_tbl"
    )

    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Empty Catalog Table",
        slug="empty_catalog_tbl",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/*.parquet",
        column_defs=[{"name": "item_id", "type": "BIGINT"}, {"name": "sku", "type": "VARCHAR"}],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    resp = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp.status_code == 200
    mig_data = resp.json()
    assert mig_data["ok"] is True

    db.refresh(tbl)
    assert tbl.format == "iceberg"
    assert tbl.iceberg is not None

    rest = env["rest"](env["writer"])
    ice_table = rest.load_table(("main_lake", "bronze", "empty_catalog_tbl"))
    assert ice_table.scan().to_arrow().num_rows == 0
    assert "item_id" in ice_table.schema().column_names
    db.close()
