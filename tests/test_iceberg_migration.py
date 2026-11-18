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
from pyiceberg.exceptions import NoSuchTableError


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


def test_rollback_migration_zero_copy(env):
    """Untouched Iceberg tables rollback zero-copy to plain Parquet format."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    table_dir = (
        env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "rollback_test_tbl"
    )
    data = pa.table({"id": pa.array([10, 20], pa.int64()), "val": ["a", "b"]})
    _write_parquet(table_dir / "data.parquet", data)

    original_pattern = f"file://{table_dir}/data.parquet"
    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Rollback Test Table",
        slug="rollback_test_tbl",
        format="parquet",
        s3_path_pattern=original_pattern,
        column_defs=[{"name": "id", "type": "BIGINT"}, {"name": "val", "type": "VARCHAR"}],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    # 1. Migrate to Iceberg
    resp_mig = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp_mig.status_code == 200
    db.refresh(tbl)
    assert tbl.format == "iceberg"

    # 2. Dry run rollback
    resp_dry = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-parquet",
        headers=headers,
        json={"dry_run": True},
    )
    assert resp_dry.status_code == 200
    dry_data = resp_dry.json()
    assert dry_data["status"] == "planned"
    assert dry_data["type"] == "zero_copy"
    assert dry_data["target_format"] == "parquet"

    # 3. Real zero-copy rollback
    resp_rb = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-parquet",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp_rb.status_code == 200
    rb_data = resp_rb.json()
    assert rb_data["status"] == "rolled_back"
    assert rb_data["type"] == "zero_copy"

    db.refresh(tbl)
    assert tbl.format == "parquet"
    assert tbl.iceberg is None
    assert tbl.s3_path_pattern == original_pattern

    # Verify metadata folder is purged and underlying Parquet file exists
    assert not (table_dir / "metadata").exists()
    assert (table_dir / "data.parquet").exists()

    # DuckDB query on restored Parquet table
    scan = table_scan.table_scan_for(tbl)
    assert not scan.startswith("iceberg_scan")
    con = duckdb.connect()
    res = con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0]
    assert res == 2
    db.close()


def test_rollback_migration_mutated_requires_materialize(env):
    """Mutated Iceberg tables require materialize=True to export back to Parquet."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    table_dir = (
        env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "mutated_rollback_tbl"
    )
    data = pa.table({"id": pa.array([1], pa.int64()), "val": ["initial"]})
    _write_parquet(table_dir / "data.parquet", data)

    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Mutated Rollback Table",
        slug="mutated_rollback_tbl",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/data.parquet",
        column_defs=[{"name": "id", "type": "BIGINT"}, {"name": "val", "type": "VARCHAR"}],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    # Migrate to Iceberg
    resp_mig = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp_mig.status_code == 200

    # Mutate table with an additional append commit
    rest = env["rest"](env["writer"])
    ice_table = rest.load_table(("main_lake", "bronze", "mutated_rollback_tbl"))
    new_data = pa.table({"id": pa.array([2], pa.int64()), "val": ["appended"]})
    ice_table.append(new_data)
    db.refresh(tbl)

    # Rollback without materialize fails / is rejected
    resp_fail = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-parquet",
        headers=headers,
        json={"dry_run": False, "materialize": False},
    )
    assert resp_fail.status_code == 400 or resp_fail.json().get("ok") is False

    # Rollback with materialize=True succeeds and exports all rows to Parquet
    resp_mat = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-parquet",
        headers=headers,
        json={"dry_run": False, "materialize": True},
    )
    assert resp_mat.status_code == 200
    mat_data = resp_mat.json()
    assert mat_data["status"] == "converted"
    assert mat_data["type"] == "materialized"

    db.refresh(tbl)
    assert tbl.format == "parquet"
    assert tbl.iceberg is None

    scan = table_scan.table_scan_for(tbl)
    con = duckdb.connect()
    res = con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0]
    assert res == 2
    db.close()


def test_rollback_catalog_all(env):
    """Batch rollback converts an entire catalog's Iceberg tables back to Parquet."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]

    table_dir = env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "cat_rb_tbl"
    data = pa.table({"id": pa.array([1, 2], pa.int64())})
    _write_parquet(table_dir / "data.parquet", data)

    tbl = CatalogTable(
        schema_id=schema_row.id,
        name="Cat RB Table",
        slug="cat_rb_tbl",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/data.parquet",
        column_defs=[{"name": "id", "type": "BIGINT"}],
    )
    db.add(tbl)
    db.commit()

    tbl_id = str(tbl.id)
    cat_id = str(cat.id)
    sch_id = str(schema_row.id)
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}

    # Migrate to Iceberg
    resp_mig = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/migrate-to-iceberg",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp_mig.status_code == 200
    db.refresh(tbl)
    assert tbl.format == "iceberg"

    # Batch rollback catalog
    resp_batch = client.post(
        f"/api/catalogs/{cat_id}/migrate-to-parquet",
        headers=headers,
        json={"dry_run": False},
    )
    assert resp_batch.status_code == 200
    batch_data = resp_batch.json()
    assert batch_data["ok"] is True
    assert batch_data["summary"]["rolled_back"] >= 1

    db.refresh(tbl)
    assert tbl.format == "parquet"
    assert tbl.iceberg is None
    db.close()


def test_create_table_migrates_table_registered_as_parquet(env):
    """A parquet-registered table (declared by a bundle) must not hit load 404 then create 409 (#570)."""
    db = env["Session"]()
    ws = db.merge(env["ws"])
    cat = db.merge(env["cat"])
    schema_row = cat.schemas[0]
    table_dir = env["root"] / "workspaces" / ws.slug / cat.slug / schema_row.slug / "declared_pq"
    _write_parquet(
        table_dir / "run_1.parquet",
        pa.table({"id": pa.array([1, 2], pa.int64()), "name": ["a", "b"]}),
    )
    db.add(
        CatalogTable(
            schema_id=schema_row.id,
            name="declared_pq",
            slug="declared_pq",
            format="parquet",
            s3_path_pattern=f"file://{table_dir}/*.parquet",
        )
    )
    db.commit()

    rest = env["rest"](env["writer"])
    ident = ("main_lake", "bronze", "declared_pq")
    with pytest.raises(NoSuchTableError):
        rest.load_table(ident)

    arrow = pa.table({"id": pa.array([3], pa.int64()), "name": ["c"]})
    ice = rest.create_table(ident, schema=arrow.schema)
    ice.append(arrow)

    assert sorted(rest.load_table(ident).scan().to_arrow()["id"].to_pylist()) == [1, 2, 3]
    db.expire_all()
    assert db.query(CatalogTable).filter_by(slug="declared_pq").one().format == "iceberg"
    db.close()
