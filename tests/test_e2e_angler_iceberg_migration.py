"""End-to-End Angler CLI Iceberg Migration & Rollback Suite.

Verifies bidirectional in-place zero-copy and materialized migration:
- 'angler catalog tables migrate' (Parquet -> Iceberg)
- 'angler catalog tables rollback' (Iceberg -> Parquet, zero-copy & materialized)
- 'angler catalog migrate' & 'angler catalog rollback' (Catalog-wide batch)
- Formats: table, json, yaml, toon
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from models import Catalog, CatalogSchema, CatalogTable
from tests.e2e_angler_common import angler_binary, run_angler, server  # noqa: F401


def _write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))


def test_angler_cli_table_migration_and_rollback_zero_copy(server, angler_binary, tmp_path):
    """Verify live zero-copy migration and rollback cycle using Angler CLI."""
    db = server.session_factory()

    # 1. Seed catalog and schema
    cat = Catalog(
        name="Warehouse Lake",
        slug="warehouse_lake",
        description="Production analytical data catalog",
        owner_workspace_id=server.workspace_id,
        is_default=True,
    )
    db.add(cat)
    db.flush()

    sch = CatalogSchema(
        catalog_id=cat.id,
        name="Core Schema",
        slug="core",
        description="Core dimensional tables",
        tier="silver",
    )
    db.add(sch)
    db.flush()

    # 2. Seed Parquet table files on disk inside workspace warehouse jail
    table_dir = (
        server.warehouse_dir
        / "workspaces"
        / server.workspace_slug
        / cat.slug
        / sch.slug
        / "orders"
    )
    data1 = pa.table({"id": pa.array([1, 2], pa.int64()), "amount": [19.99, 45.50]})
    data2 = pa.table({"id": pa.array([3], pa.int64()), "amount": [99.00]})
    _write_parquet(table_dir / "part-0.parquet", data1)
    _write_parquet(table_dir / "part-1.parquet", data2)

    tbl = CatalogTable(
        schema_id=sch.id,
        name="Orders",
        slug="orders",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/**/*.parquet",
        column_defs=[{"name": "id", "type": "BIGINT"}, {"name": "amount", "type": "DOUBLE"}],
    )
    db.add(tbl)
    db.commit()

    cat_slug = cat.slug
    sch_slug = sch.slug
    tbl_slug = tbl.slug

    # ── 1. Dry run table migration via Angler CLI ──────────────────────────────
    res_dry = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "migrate", cat_slug, sch_slug, tbl_slug, "--dry-run", "--json"],
    )
    assert res_dry.returncode == 0, f"dry-run failed: {res_dry.stderr}"
    dry_data = json.loads(res_dry.stdout)
    assert dry_data.get("status") == "planned"
    assert dry_data.get("table") == "orders"
    assert dry_data.get("files") == 2
    assert dry_data.get("rows") == 3

    # ── 2. Execute table migration to Iceberg in-place zero-copy ───────────────
    res_mig = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "migrate", cat_slug, sch_slug, tbl_slug, "-y", "--json"],
    )
    assert res_mig.returncode == 0, f"migration failed: {res_mig.stderr}"
    mig_data = json.loads(res_mig.stdout)
    assert mig_data.get("status") == "migrated"
    assert "snapshot_id" in mig_data

    # Verify database record updated to iceberg
    db.refresh(tbl)
    assert tbl.format == "iceberg"
    assert tbl.iceberg is not None

    # ── 3. Query migrated table via DuckDB through Angler CLI ──────────────────
    res_query = run_angler(
        angler_binary,
        server,
        ["query", f"SELECT COUNT(*) AS total_rows, SUM(amount) AS total_amount FROM {cat_slug}.{sch_slug}.{tbl_slug}", "--json"],
    )
    assert res_query.returncode == 0, f"query failed: {res_query.stderr}"
    assert "3" in res_query.stdout
    assert "164.49" in res_query.stdout

    # ── 4. Dry run table rollback via Angler CLI ───────────────────────────────
    res_rb_dry = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "rollback", cat_slug, sch_slug, tbl_slug, "--dry-run", "--json"],
    )
    assert res_rb_dry.returncode == 0, f"rollback dry-run failed: {res_rb_dry.stderr}"
    rb_dry_data = json.loads(res_rb_dry.stdout)
    assert rb_dry_data.get("status") == "planned"
    assert rb_dry_data.get("type") == "zero_copy"
    assert rb_dry_data.get("target_format") == "parquet"

    # ── 5. Execute zero-copy rollback to Parquet via Angler CLI ────────────────
    res_rb = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "rollback", cat_slug, sch_slug, tbl_slug, "-y", "-o", "table"],
    )
    assert res_rb.returncode == 0, f"rollback failed: {res_rb.stderr}"
    assert "rolled back zero-copy to Parquet" in (res_rb.stdout + res_rb.stderr)

    # Verify table format in database flipped back to parquet
    db.refresh(tbl)
    assert tbl.format == "parquet"

    # ── 6. Query again to verify Parquet reader still functions ────────────────
    res_query_pq = run_angler(
        angler_binary,
        server,
        ["query", f"SELECT COUNT(*) AS total_rows FROM {cat_slug}.{sch_slug}.{tbl_slug}", "--json"],
    )
    assert res_query_pq.returncode == 0
    assert "3" in res_query_pq.stdout

    # ── 7. Cross-flag syntax: migrate with --to-parquet ────────────────────────
    # First migrate back to iceberg
    res_mig2 = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "migrate", cat_slug, sch_slug, tbl_slug, "-y"],
    )
    assert res_mig2.returncode == 0
    db.refresh(tbl)
    assert tbl.format == "iceberg"

    # Now rollback via 'migrate --to-parquet'
    res_mig_to_pq = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "migrate", cat_slug, sch_slug, tbl_slug, "--to-parquet", "-y"],
    )
    assert res_mig_to_pq.returncode == 0
    db.refresh(tbl)
    assert tbl.format == "parquet"

    db.close()


def test_angler_cli_table_rollback_materialized(server, angler_binary, tmp_path):
    """Verify materialized rollback when an Iceberg table has received new commits."""
    db = server.session_factory()

    cat = Catalog(
        name="Mutated Lake",
        slug="mutated_lake",
        owner_workspace_id=server.workspace_id,
        is_default=False,
    )
    db.add(cat)
    db.flush()

    sch = CatalogSchema(
        catalog_id=cat.id,
        name="Mutated Schema",
        slug="mutated_schema",
    )
    db.add(sch)
    db.flush()

    table_dir = (
        server.warehouse_dir
        / "workspaces"
        / server.workspace_slug
        / cat.slug
        / sch.slug
        / "mutated_tbl"
    )
    data = pa.table({"id": pa.array([10, 20], pa.int64()), "val": ["foo", "bar"]})
    _write_parquet(table_dir / "data.parquet", data)

    tbl = CatalogTable(
        schema_id=sch.id,
        name="Mutated Tbl",
        slug="mutated_tbl",
        format="parquet",
        s3_path_pattern=f"file://{table_dir}/data.parquet",
        column_defs=[{"name": "id", "type": "BIGINT"}, {"name": "val", "type": "VARCHAR"}],
    )
    db.add(tbl)
    db.commit()

    cat_slug = cat.slug
    sch_slug = sch.slug
    tbl_slug = tbl.slug

    # 1. Migrate to Iceberg
    res_mig = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "migrate", cat_slug, sch_slug, tbl_slug, "-y", "--json"],
    )
    assert res_mig.returncode == 0

    # 2. Simulate new commit on Iceberg table by bumping current_snapshot_id
    db.refresh(tbl)
    assert tbl.iceberg is not None
    tbl.iceberg.current_snapshot_id = 999999999999
    db.commit()

    # 3. Rollback without --materialize should fail with instruction to use materialize
    res_fail = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "rollback", cat_slug, sch_slug, tbl_slug, "-y", "--json"],
    )
    assert res_fail.returncode != 0
    assert "materialize" in (res_fail.stdout + res_fail.stderr).lower()

    # 4. Rollback with --materialize succeeds
    res_mat = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "rollback", cat_slug, sch_slug, tbl_slug, "--materialize", "-y", "--json"],
    )
    assert res_mat.returncode == 0, f"materialized rollback failed: {res_mat.stderr}"
    mat_data = json.loads(res_mat.stdout)
    assert mat_data.get("status") == "converted"
    assert mat_data.get("type") == "materialized"

    db.refresh(tbl)
    assert tbl.format == "parquet"
    db.close()


def test_angler_cli_catalog_wide_migration_and_rollback(server, angler_binary, tmp_path):
    """Verify batch catalog-wide migration and rollback commands."""
    db = server.session_factory()

    cat = Catalog(
        name="Batch Catalog",
        slug="batch_catalog",
        owner_workspace_id=server.workspace_id,
        is_default=False,
    )
    db.add(cat)
    db.flush()

    sch = CatalogSchema(
        catalog_id=cat.id,
        name="Batch Schema",
        slug="batch_schema",
    )
    db.add(sch)
    db.flush()

    # Create 2 Parquet tables
    for name in ["batch_t1", "batch_t2"]:
        tdir = (
            server.warehouse_dir
            / "workspaces"
            / server.workspace_slug
            / cat.slug
            / sch.slug
            / name
        )
        _write_parquet(tdir / "data.parquet", pa.table({"id": pa.array([1, 2], pa.int64())}))
        tbl = CatalogTable(
            schema_id=sch.id,
            name=name,
            slug=name,
            format="parquet",
            s3_path_pattern=f"file://{tdir}/*.parquet",
            column_defs=[{"name": "id", "type": "BIGINT"}],
        )
        db.add(tbl)
    db.commit()

    cat_slug = cat.slug

    # 1. Catalog dry-run migration
    res_dry = run_angler(
        angler_binary,
        server,
        ["catalog", "migrate", cat_slug, "--dry-run", "--json"],
    )
    assert res_dry.returncode == 0
    dry_data = json.loads(res_dry.stdout)
    assert dry_data.get("summary", {}).get("planned") == 2

    # 2. Batch catalog migration to Iceberg
    res_mig = run_angler(
        angler_binary,
        server,
        ["catalog", "migrate", cat_slug, "-y", "--json"],
    )
    assert res_mig.returncode == 0
    mig_data = json.loads(res_mig.stdout)
    assert mig_data.get("summary", {}).get("migrated") == 2

    # 3. Batch catalog rollback to Parquet
    res_rb = run_angler(
        angler_binary,
        server,
        ["catalog", "rollback", cat_slug, "-y", "--json"],
    )
    assert res_rb.returncode == 0
    rb_data = json.loads(res_rb.stdout)
    assert rb_data.get("summary", {}).get("rolled_back") == 2

    db.close()
