"""Tests for Phase 3: Snapshot history, time-travel queries, RESTORE and MCP/Genie TOON integration (#546)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb
import pyarrow as pa
import table_scan
from iceberg_testkit import _REPO_ROOT, client, iceberg_env  # noqa: F401
from models import CatalogTable

ARROW_1 = pa.table({"id": pa.array([1, 2], pa.int64()), "val": ["v1", "v2"]})
ARROW_2 = pa.table({"id": pa.array([3], pa.int64()), "val": ["v3"]})
ARROW_3 = pa.table({"id": pa.array([4, 5], pa.int64()), "val": ["v4", "v5"]})


def test_snapshot_history_and_restore_endpoints(env):
    """Snapshot history endpoint lists all snapshots with metadata, and restore rolls back to any snapshot."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "history_tbl"), ARROW_1.schema)

    # Snapshot 1
    table.append(ARROW_1)
    s1_id = table.current_snapshot().snapshot_id

    # Snapshot 2
    table.append(ARROW_2)
    s2_id = table.current_snapshot().snapshot_id

    # Snapshot 3
    table.append(ARROW_3)
    s3_id = table.current_snapshot().snapshot_id

    assert s1_id != s2_id != s3_id

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="history_tbl").one()
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}
    cat_id = str(env["cat"].id)
    sch_id = str(row.schema_id)
    tbl_id = str(row.id)

    # 1. Fetch snapshot history
    resp_snap = client.get(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/snapshots",
        headers=headers,
    )
    assert resp_snap.status_code == 200
    snap_data = resp_snap.json()
    assert snap_data["snapshot_count"] == 3
    assert snap_data["current_snapshot_id"] == str(s3_id)
    snapshots = snap_data["snapshots"]
    assert len(snapshots) == 3
    assert snapshots[0]["is_current"] is True
    assert snapshots[0]["snapshot_id"] == str(s3_id)

    # 2. RESTORE table to Snapshot 1
    resp_restore = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/restore",
        headers=headers,
        json={"snapshot_id": str(s1_id)},
    )
    assert resp_restore.status_code == 200
    restore_data = resp_restore.json()
    assert restore_data["restored"] is True
    assert restore_data["snapshot_id"] == str(s1_id)

    # Verify table now reads 2 rows (as in Snapshot 1)
    reloaded = rest.load_table(("main_lake", "bronze", "history_tbl"))
    assert reloaded.current_snapshot().snapshot_id == s1_id
    assert reloaded.scan().to_arrow().num_rows == 2

    # 3. RESTORE table forward to Snapshot 2
    resp_restore2 = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/restore",
        headers=headers,
        json={"snapshot_id": str(s2_id)},
    )
    assert resp_restore2.status_code == 200
    assert reloaded.refresh().scan().to_arrow().num_rows == 3

    # 4. Attempt restore with non-existent snapshot ID fails gracefully with 400
    resp_bad = client.post(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/restore",
        headers=headers,
        json={"snapshot_id": "999999999999999"},
    )
    assert resp_bad.status_code == 400
    assert "does not exist" in resp_bad.text
    db.close()


def test_time_travel_sql_queries_duckdb(env):
    """DuckDB SQL queries with FOR SYSTEM_VERSION AS OF and FOR SYSTEM_TIME AS OF resolve correctly."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "timetravel_tbl"), ARROW_1.schema)

    # Snapshot 1
    table.append(ARROW_1)
    s1_id = table.current_snapshot().snapshot_id
    s1_ts_ms = table.current_snapshot().timestamp_ms
    s1_iso = datetime.fromtimestamp(s1_ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")

    # Snapshot 2
    table.append(ARROW_2)

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="timetravel_tbl").one()

    catalogs = [
        {
            "slug": "main_lake",
            "name": "Main Lake",
            "schemas": [
                {
                    "slug": "bronze",
                    "tables": [
                        {
                            "slug": "timetravel_tbl",
                            "name": "timetravel_tbl",
                            "format": "iceberg",
                            "iceberg_metadata_location": row.iceberg.metadata_location,
                            "s3_path_pattern": row.iceberg.location,
                        }
                    ],
                }
            ],
        }
    ]

    # Test Version As Of query rewrite
    sql_v1 = f"SELECT count(*) FROM bronze.timetravel_tbl FOR SYSTEM_VERSION AS OF {s1_id}"
    rewritten_sql, views = table_scan.rewrite_time_travel(sql_v1, catalogs)
    assert len(views) == 1
    assert "snapshot_from_id = " in views[0]

    # Execute in DuckDB
    con = duckdb.connect()
    assert table_scan.ensure_iceberg_loaded(con)
    table_scan.apply_time_travel_views(con, views)
    count_s1 = con.execute(rewritten_sql).fetchone()[0]
    assert count_s1 == 2

    # Test Timestamp As Of query rewrite
    sql_ts = f"SELECT count(*) FROM bronze.timetravel_tbl TIMESTAMP AS OF '{s1_iso}'"
    rewritten_ts, views_ts = table_scan.rewrite_time_travel(sql_ts, catalogs)
    assert len(views_ts) == 1
    assert "snapshot_from_timestamp = " in views_ts[0]

    # Query current without time travel
    scan_current = table_scan.table_scan_for(
        {
            "format": "iceberg",
            "s3_path_pattern": row.iceberg.location,
            "iceberg_metadata_location": row.iceberg.metadata_location,
        }
    )
    count_cur = con.execute(f"SELECT count(*) FROM {scan_current}").fetchone()[0]
    assert count_cur == 3
    db.close()


def test_sql_restore_table_statement_interception(env):
    """RESTORE TABLE <catalog>.<schema>.<table> TO SNAPSHOT <id> is intercepted and executed via SQL Editor."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "sql_restore_tbl"), ARROW_1.schema)

    table.append(ARROW_1)
    s1_id = table.current_snapshot().snapshot_id

    table.append(ARROW_2)

    assert rest.load_table(("main_lake", "bronze", "sql_restore_tbl")).scan().to_arrow().num_rows == 3

    # Intercept via sql_router
    import iceberg_service
    db = env["Session"]()
    ws = db.merge(env["ws"])
    writer_user = db.merge(env["writer"])

    sql = f"RESTORE TABLE bronze.sql_restore_tbl TO SNAPSHOT {s1_id}"
    handled, msg = iceberg_service.try_handle_restore_statement(
        sql=sql,
        workspace=ws,
        user=writer_user,
        default_schema="bronze",
        db=db,
    )
    assert handled is True
    assert "Restored sql_restore_tbl to snapshot" in msg

    # Verify state rolled back to snapshot 1
    reloaded = rest.load_table(("main_lake", "bronze", "sql_restore_tbl"))
    assert reloaded.current_snapshot().snapshot_id == s1_id
    assert reloaded.scan().to_arrow().num_rows == 2
    db.close()


def test_mcp_describe_table_iceberg_toon_integration(env):
    """LoT Native MCP describe_table returns format='iceberg' and Tier 1 toon_summary."""
    from backend.mcp_router import _execute_mcp_tool

    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "mcp_ice_tbl"), ARROW_1.schema)
    table.append(ARROW_1)

    db = env["Session"]()
    writer_user = db.merge(env["writer"])

    res = _execute_mcp_tool(
        db=db,
        current_user=writer_user,
        tool_name="describe_table",
        arguments={
            "catalog": "main_lake",
            "schema": "bronze",
            "table": "mcp_ice_tbl",
            "workspace_slug": "dev",
            "include_files_manifest": True,
        },
    )
    assert res.get("isError") is False
    data = json.loads(res["content"][0]["text"])
    assert data["ok"] is True
    assert data["format"] == "iceberg"
    assert data["toon_summary"] is not None
    assert "table: mcp_ice_tbl" in data["toon_summary"]
    assert data["toon_files"] is not None
    db.close()
