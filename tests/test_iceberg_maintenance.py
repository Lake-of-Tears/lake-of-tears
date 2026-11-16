"""Tests for Phase 4: Iceberg table maintenance engine (compaction, snapshot expiration & orphan cleanup) (#547)."""

from __future__ import annotations

import pyarrow as pa
from iceberg_testkit import _REPO_ROOT, client, iceberg_env  # noqa: F401
from models import CatalogTable


def _batch(start: int, count: int) -> pa.Table:
    return pa.table(
        {
            "id": pa.array(range(start, start + count), pa.int64()),
            "val": [f"val_{i}" for i in range(start, start + count)],
        }
    )


def test_maintenance_policy_api(env):
    """GET and PUT /iceberg/maintenance endpoints inspect and update per-table maintenance settings."""
    rest = env["rest"](env["writer"])
    rest.create_table(("main_lake", "bronze", "policy_tbl"), _batch(0, 1).schema)

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="policy_tbl").one()
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}
    cat_id = str(env["cat"].id)
    sch_id = str(row.schema_id)
    tbl_id = str(row.id)

    # 1. GET default policy
    resp_get = client.get(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/iceberg/maintenance",
        headers=headers,
    )
    assert resp_get.status_code == 200
    policy = resp_get.json()["policy"]
    assert policy["enabled"] is True
    assert policy["compaction_enabled"] is True
    assert policy["compaction_min_files"] == 5

    # 2. PUT custom policy
    resp_put = client.put(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/iceberg/maintenance",
        headers=headers,
        json={"compaction_min_files": 2, "snapshot_retention_days": 14},
    )
    assert resp_put.status_code == 200
    updated = resp_put.json()["policy"]
    assert updated["compaction_min_files"] == 2
    assert updated["snapshot_retention_days"] == 14
    db.close()


def test_compaction_optimize_rewrites_small_files(env):
    """OPTIMIZE compacts accumulated small files into fewer data files while preserving all records."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "compact_tbl"), _batch(0, 1).schema)

    # Append 4 tiny batches
    for i in range(4):
        table.append(_batch(i * 10, 10))

    assert table.scan().to_arrow().num_rows == 40
    data_files_before = list(table.current_snapshot().manifests(table.io))
    assert len(data_files_before) >= 1

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="compact_tbl").one()
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}
    cat_id = str(env["cat"].id)
    sch_id = str(row.schema_id)
    tbl_id = str(row.id)

    # Dry-run check
    resp_dry = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/iceberg/optimize",
        headers=headers,
        json={"dry_run": True, "compaction_min_files": 2},
    )
    assert resp_dry.status_code == 200
    assert resp_dry.json()["dry_run"] is True

    # Real compaction execution
    resp_opt = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/iceberg/optimize",
        headers=headers,
        json={"dry_run": False, "compaction_min_files": 2},
    )
    assert resp_opt.status_code == 200
    opt_data = resp_opt.json()
    assert opt_data["action"] == "compact"
    assert opt_data["files_rewritten"] >= 1

    # Verify all 40 records are intact
    reloaded = rest.load_table(("main_lake", "bronze", "compact_tbl"))
    assert reloaded.scan().to_arrow().num_rows == 40
    db.close()


def test_vacuum_snapshot_expiration_and_orphan_cleanup(env):
    """VACUUM drops expired snapshots and purges unreferenced orphan files."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "vacuum_tbl"), _batch(0, 1).schema)

    # Create 3 snapshots
    table.append(_batch(0, 5))
    table.append(_batch(5, 5))
    table.append(_batch(10, 5))
    s3 = table.current_snapshot().snapshot_id

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="vacuum_tbl").one()
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}
    cat_id = str(env["cat"].id)
    sch_id = str(row.schema_id)
    tbl_id = str(row.id)

    # VACUUM retaining only the last 1 snapshot
    resp_vac = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/iceberg/vacuum",
        headers=headers,
        json={
            "dry_run": False,
            "snapshot_retention_days": 0,
            "retain_last_snapshots": 1,
            "orphan_older_than_hours": 0,
        },
    )
    assert resp_vac.status_code == 200
    vac_data = resp_vac.json()
    assert vac_data["ok"] is True
    assert vac_data["action"] == "vacuum"
    assert "expire_snapshots" in vac_data
    assert "remove_orphan_files" in vac_data

    # Verify current snapshot is s3 and data is intact
    reloaded = rest.load_table(("main_lake", "bronze", "vacuum_tbl"))
    assert reloaded.current_snapshot().snapshot_id == s3
    assert reloaded.scan().to_arrow().num_rows == 15
    db.close()
