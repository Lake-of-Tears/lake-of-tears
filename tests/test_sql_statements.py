"""Comprehensive tests for the Statement Execution REST API (/api/sql/statements) — Issue #347."""

from __future__ import annotations

import importlib.util
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import jwt
import pytest
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import sql_router  # noqa: E402
from models import (  # noqa: E402
    Catalog,
    CatalogSchema,
    CatalogTable,
    User,
    Workspace,
    WorkspaceMember,
)
from sql_guardrails import (  # noqa: E402
    is_s3_write_path_allowed,
    is_write_sql,
    validate_statement_guardrails,
)


def _make_auth_context(
    role: str = "write",
    ws_slug: str = "analytics",
    is_superadmin: bool = False,
):
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}@lake.local",
        display_name=role.capitalize(),
        role="superadmin" if is_superadmin else "user",
        is_active=True,
    )
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    ws_id = uuid.uuid4()
    workspace = Workspace(
        id=ws_id,
        name=f"{ws_slug.capitalize()} Workspace",
        slug=ws_slug,
        status="active",
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    return user, token, workspace, member


def _build_mock_db(workspace: Workspace, member: WorkspaceMember, catalog: Catalog | None = None):
    mock_db = MagicMock()
    added_objects: list = []
    deleted_objects: list = []

    def add_side_effect(obj):
        added_objects.append(obj)

    def delete_side_effect(obj):
        deleted_objects.append(obj)

    mock_db.add.side_effect = add_side_effect
    mock_db.delete.side_effect = delete_side_effect

    def query_side_effect(model, *args, **kwargs):
        q = MagicMock()
        if model is Workspace:
            q.filter.return_value.first.return_value = workspace
            q.filter.return_value.order_by.return_value.first.return_value = workspace
        elif model is WorkspaceMember:
            q.filter.return_value.first.return_value = member
        elif model is Catalog:
            q.filter.return_value.order_by.return_value.all.return_value = (
                [catalog] if catalog else []
            )
            q.filter.return_value.order_by.return_value.first.return_value = catalog
        elif model is CatalogSchema:
            q.filter_by.return_value.first.return_value = None
        elif model is CatalogTable:
            q.filter_by.return_value.first.return_value = None
        else:
            q.filter.return_value.order_by.return_value.all.return_value = []
            q.filter.return_value.all.return_value = []
        return q

    mock_db.query.side_effect = query_side_effect
    return mock_db, added_objects, deleted_objects


class TestStatementGuardrailsUnit:
    def test_is_write_sql_classification(self):
        assert is_write_sql("SELECT * FROM telemetry LIMIT 10") is False
        assert is_write_sql("WITH cte AS (SELECT 1) SELECT * FROM cte") is False
        assert is_write_sql("DESCRIBE telemetry") is False
        assert is_write_sql("SHOW TABLES") is False

        assert is_write_sql("CREATE TABLE t AS SELECT 1 AS id") is True
        assert is_write_sql("CREATE OR REPLACE TABLE bronze.t (id INT)") is True
        assert is_write_sql("INSERT INTO bronze.t VALUES (1)") is True
        assert is_write_sql("UPDATE bronze.t SET id = 2 WHERE id = 1") is True
        assert is_write_sql("DELETE FROM bronze.t WHERE id = 1") is True
        assert is_write_sql("ALTER TABLE bronze.t ADD COLUMN name VARCHAR") is True
        assert is_write_sql("DROP TABLE bronze.t") is True
        assert is_write_sql("COPY (SELECT 1) TO 's3://datalake/workspaces/ws1/out.parquet'") is True

    def test_host_filesystem_access_blocked(self):
        illegal_queries = [
            "SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM read_parquet('./local_data.parquet')",
            "SELECT * FROM read_text('../secrets.txt')",
            "SELECT * FROM 'file:///etc/hosts'",
            "SELECT * FROM '/var/log/syslog.parquet'",
            "COPY (SELECT 1) TO '/tmp/exfiltrate.parquet'",
            "COPY (SELECT 1) TO './dump.csv'",
            "SELECT * FROM read_parquet('https://evil.example.com/payload.parquet')",
        ]
        for sql in illegal_queries:
            allowed, err = validate_statement_guardrails(
                sql=sql,
                workspace_slug="analytics",
                is_superadmin=False,
                bucket="datalake",
            )
            assert allowed is False, f"Expected host/network path in {sql!r} to be blocked"
            assert "Access Denied" in (err or "")

    def test_dangerous_duckdb_commands_blocked(self):
        dangerous_queries = [
            "ATTACH '/tmp/evil.db' AS evil",
            "INSTALL httpfs",
            "LOAD '/tmp/malicious.duckdb_extension'",
            "PRAGMA database_list",
            "SET s3_endpoint='evil.example.com'",
            "EXPORT DATABASE '/tmp/dump'",
            "IMPORT DATABASE '/tmp/dump'",
        ]
        for sql in dangerous_queries:
            allowed, err = validate_statement_guardrails(
                sql=sql,
                workspace_slug="analytics",
                is_superadmin=False,
                bucket="datalake",
            )
            assert allowed is False, f"Expected command {sql!r} to be blocked"
            assert "Access Denied" in (err or "")

    def test_s3_write_path_isolation(self):
        # Allowed write to active workspace prefix
        ok, err = is_s3_write_path_allowed(
            "s3://datalake/workspaces/analytics/gold/metrics.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert ok is True
        assert err is None

        # Blocked write to another workspace
        ok2, err2 = is_s3_write_path_allowed(
            "s3://datalake/workspaces/finance/gold/metrics.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert ok2 is False
        assert "Access Denied" in (err2 or "")

        # Blocked write to system raw/ prefix (read-only for workspaces)
        ok3, err3 = is_s3_write_path_allowed(
            "s3://datalake/raw/telemetry/overwrite.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert ok3 is False
        assert "Access Denied" in (err3 or "")

        # COPY TO raw/ blocked by validate_statement_guardrails
        ok4, err4 = validate_statement_guardrails(
            sql="COPY (SELECT 1) TO 's3://datalake/raw/hack.parquet'",
            workspace_slug="analytics",
            is_superadmin=False,
            bucket="datalake",
        )
        assert ok4 is False
        assert "Access Denied" in (err4 or "")


class TestStatementExecutionEndpoints:
    @pytest.fixture(autouse=True)
    def setup_app(self):
        sql_router.statement_store.clear()
        spec_backend = importlib.util.spec_from_file_location(
            "backend_main_sql_test", Path(__file__).parents[1] / "backend" / "main.py"
        )
        self.backend_main = importlib.util.module_from_spec(spec_backend)
        sys.modules["backend_main_sql_test"] = self.backend_main
        spec_backend.loader.exec_module(self.backend_main)
        self.client = TestClient(self.backend_main.app)
        yield
        self.backend_main.app.dependency_overrides.clear()
        sql_router.statement_store.clear()

    def _override_context(
        self, role: str = "write", ws_slug: str = "analytics", is_superadmin: bool = False
    ):
        user, token, ws, member = _make_auth_context(
            role=role, ws_slug=ws_slug, is_superadmin=is_superadmin
        )
        mock_db, added, deleted = _build_mock_db(ws, member)
        self.backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
        self.backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user
        return user, token, ws, mock_db, added, deleted

    def test_execute_select_statement_databricks_schema(self):
        _, token, ws, _, _, _ = self._override_context(role="read", ws_slug="analytics")

        resp = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT 42 AS id, 'sensor_alpha' AS label",
                "warehouse_id": "default",
                "catalog": "main",
                "schema": "default",
                "parameters": [],
                "row_limit": 1000,
                "byte_limit": 10485760,
                "format": "JSON_ARRAY",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "statement_id" in data
        assert data["status"]["state"] == "SUCCEEDED"
        assert data["manifest"]["format"] == "JSON_ARRAY"
        assert data["manifest"]["total_row_count"] == 1
        assert data["manifest"]["total_byte_count"] > 0
        cols = data["manifest"]["schema"]["columns"]
        assert len(cols) == 2
        assert cols[0]["name"] == "id"
        assert cols[1]["name"] == "label"
        assert data["result"]["data_array"] == [[42, "sensor_alpha"]]
        assert data["result"]["next_chunk_index"] is None

    def test_execute_with_named_and_positional_parameters(self):
        _, token, ws, _, _, _ = self._override_context(role="read", ws_slug="analytics")

        # 1. Named parameters (:dept and :min_val)
        resp_named = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT :dept AS department, :min_val + 5 AS score",
                "parameters": [
                    {"name": "dept", "value": "engineering", "type": "VARCHAR"},
                    {"name": "min_val", "value": "95", "type": "INTEGER"},
                ],
            },
        )
        assert resp_named.status_code == 200
        body_named = resp_named.json()
        assert body_named["status"]["state"] == "SUCCEEDED"
        assert body_named["result"]["data_array"] == [["engineering", 100]]

        # 2. Positional parameters (?)
        resp_pos = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT ? AS region, ? * 2 AS doubled",
                "parameters": ["us-east", 21],
            },
        )
        assert resp_pos.status_code == 200
        body_pos = resp_pos.json()
        assert body_pos["status"]["state"] == "SUCCEEDED"
        assert body_pos["result"]["data_array"] == [["us-east", 42]]

    def test_result_chunking_and_pagination_endpoint(self):
        _, token, ws, _, _, _ = self._override_context(role="read", ws_slug="analytics")

        resp = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT i AS id, 'row_' || CAST(i AS VARCHAR) AS val FROM range(1, 26) t(i)",
                "row_limit": 100,
                "chunk_size": 10,
                "format": "JSON_ARRAY",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        stmt_id = data["statement_id"]
        assert data["status"]["state"] == "SUCCEEDED"
        assert data["manifest"]["total_row_count"] == 25
        assert data["manifest"]["total_chunk_count"] == 3
        assert len(data["result"]["data_array"]) == 10
        assert data["result"]["chunk_index"] == 0
        assert data["result"]["next_chunk_index"] == 1

        # Fetch chunk 1
        chunk1_resp = self.client.get(
            f"/api/sql/statements/{stmt_id}/result/chunks/1",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
        )
        assert chunk1_resp.status_code == 200
        chunk1 = chunk1_resp.json()
        assert chunk1["chunk_index"] == 1
        assert chunk1["row_offset"] == 10
        assert chunk1["row_count"] == 10
        assert chunk1["next_chunk_index"] == 2
        assert chunk1["data_array"][0] == [11, "row_11"]

        # Fetch chunk 2 (final chunk)
        chunk2_resp = self.client.get(
            f"/api/sql/statements/{stmt_id}/result/chunks/2",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
        )
        assert chunk2_resp.status_code == 200
        chunk2 = chunk2_resp.json()
        assert chunk2["chunk_index"] == 2
        assert chunk2["row_offset"] == 20
        assert chunk2["row_count"] == 5
        assert chunk2["next_chunk_index"] is None
        assert chunk2["data_array"][-1] == [25, "row_25"]

        # Out-of-bounds chunk returns 404
        chunk3_resp = self.client.get(
            f"/api/sql/statements/{stmt_id}/result/chunks/99",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
        )
        assert chunk3_resp.status_code == 404

    def test_formats_json_object_and_csv(self):
        _, token, ws, _, _, _ = self._override_context(role="read", ws_slug="analytics")

        # JSON_OBJECT
        resp_obj = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT 1 AS id, 'alpha' AS name",
                "format": "JSON_OBJECT",
            },
        )
        assert resp_obj.status_code == 200
        assert resp_obj.json()["result"]["data_objects"] == [{"id": 1, "name": "alpha"}]

        # CSV
        resp_csv = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT 1 AS id, 'alpha' AS name",
                "format": "CSV",
            },
        )
        assert resp_csv.status_code == 200
        csv_text = resp_csv.json()["result"]["csv_data"]
        assert "id,name" in csv_text
        assert "1,alpha" in csv_text

    def test_ddl_ctas_persists_catalog_table_and_enforces_write_rbac(self):
        # 1. Read-only viewer cannot execute CTAS
        _, viewer_token, ws, _, _, _ = self._override_context(role="read", ws_slug="analytics")
        resp_forbidden = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "CREATE TABLE bronze.telemetry_rollup AS SELECT 1 AS sensor_id, 99.5 AS temp",
                "catalog": "main",
                "schema": "bronze",
            },
        )
        assert resp_forbidden.status_code == 403
        assert "read-only" in resp_forbidden.json()["detail"].lower()

        # 2. Writer role can execute CTAS and auto-syncs CatalogTable metadata
        _, writer_token, ws2, _, added_objs, _ = self._override_context(
            role="write", ws_slug="analytics"
        )
        resp_ctas = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {writer_token}", "X-Workspace-Slug": ws2.slug},
            json={
                "statement": "CREATE TABLE bronze.telemetry_rollup AS SELECT 1 AS sensor_id, 99.5 AS temp",
                "catalog": "main",
                "schema": "bronze",
            },
        )
        assert resp_ctas.status_code == 200
        body = resp_ctas.json()
        assert body["status"]["state"] == "SUCCEEDED"

        created_tables = [o for o in added_objs if isinstance(o, CatalogTable)]
        assert len(created_tables) == 1
        tbl = created_tables[0]
        assert tbl.slug == "telemetry_rollup"
        assert (
            tbl.s3_path_pattern
            == "s3://datalake/workspaces/analytics/main/bronze/telemetry_rollup/data.parquet"
        )
        col_names = [c["name"] for c in tbl.column_defs]
        assert "sensor_id" in col_names
        assert "temp" in col_names

    def test_statement_cancellation_and_history(self):
        _, token, ws, _, _, _ = self._override_context(role="write", ws_slug="analytics")

        # Execute a statement
        resp = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={"statement": "SELECT 100 AS metric"},
        )
        assert resp.status_code == 200
        stmt_id = resp.json()["statement_id"]

        # Get statement status by ID
        get_resp = self.client.get(
            f"/api/sql/statements/{stmt_id}",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["statement_id"] == stmt_id

        # List workspace statement history
        list_resp = self.client.get(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
        )
        assert list_resp.status_code == 200
        history = list_resp.json()
        assert len(history) >= 1
        assert history[0]["statement_id"] == stmt_id

        # Create a RUNNING statement and cancel it via POST /api/sql/statements/{id}/cancel
        running_stmt = sql_router.StoredStatement(
            statement_id=str(uuid.uuid4()),
            workspace_id=str(ws.id),
            workspace_slug=ws.slug,
            user_id=str(uuid.uuid4()),
            statement="SELECT * FROM range(100000000)",
            catalog="main",
            schema_name="default",
            warehouse_id="default",
            format="JSON_ARRAY",
            status=sql_router.StatementStatus(state="RUNNING"),
        )
        mock_con = MagicMock()
        running_stmt.connection = mock_con
        sql_router.statement_store.put(running_stmt)

        cancel_resp = self.client.post(
            f"/api/sql/statements/{running_stmt.statement_id}/cancel",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"]["state"] == "CANCELED"
        assert running_stmt.cancel_event.is_set() is True
        mock_con.interrupt.assert_called_once()

    def test_multi_tenant_isolation_blocks_cross_workspace_access(self):
        # Workspace A creates a statement
        _, token_a, ws_a, _, _, _ = self._override_context(role="write", ws_slug="ws-alpha")
        resp_a = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token_a}", "X-Workspace-Slug": ws_a.slug},
            json={"statement": "SELECT 'alpha_secret' AS secret"},
        )
        assert resp_a.status_code == 200
        stmt_id_a = resp_a.json()["statement_id"]

        # Switch to Workspace B
        _, token_b, ws_b, _, _, _ = self._override_context(role="write", ws_slug="ws-beta")

        # 1. Workspace B cannot read Workspace A's statement status
        resp_get = self.client.get(
            f"/api/sql/statements/{stmt_id_a}",
            headers={"Authorization": f"Bearer {token_b}", "X-Workspace-Slug": ws_b.slug},
        )
        assert resp_get.status_code == 403

        # 2. Workspace B cannot fetch Workspace A's result chunks
        resp_chunk = self.client.get(
            f"/api/sql/statements/{stmt_id_a}/result/chunks/0",
            headers={"Authorization": f"Bearer {token_b}", "X-Workspace-Slug": ws_b.slug},
        )
        assert resp_chunk.status_code == 403

        # 3. Workspace B cannot cancel Workspace A's statement
        resp_cancel = self.client.post(
            f"/api/sql/statements/{stmt_id_a}/cancel",
            headers={"Authorization": f"Bearer {token_b}", "X-Workspace-Slug": ws_b.slug},
        )
        assert resp_cancel.status_code == 403

        # 4. Workspace B cannot query Workspace A's S3 bucket prefix
        resp_s3_read = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token_b}", "X-Workspace-Slug": ws_b.slug},
            json={
                "statement": "SELECT * FROM read_parquet('s3://datalake/workspaces/ws-alpha/main/default/tbl/data.parquet')"
            },
        )
        assert resp_s3_read.status_code == 403

        # 5. Workspace B cannot write to Workspace A's S3 bucket prefix
        resp_s3_write = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token_b}", "X-Workspace-Slug": ws_b.slug},
            json={
                "statement": "COPY (SELECT 1) TO 's3://datalake/workspaces/ws-alpha/injected.parquet'"
            },
        )
        assert resp_s3_write.status_code == 403

        # 6. Host filesystem reads/writes are blocked with 403
        resp_host = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token_b}", "X-Workspace-Slug": ws_b.slug},
            json={"statement": "SELECT * FROM read_csv('/etc/passwd')"},
        )
        assert resp_host.status_code == 403

    def test_statement_execution_benchmark_throughput(self):
        """Benchmark analytical aggregation and chunked serialization over 5,000 rows."""
        _, token, ws, _, _, _ = self._override_context(role="read", ws_slug="analytics")

        t0 = time.perf_counter()
        resp = self.client.post(
            "/api/sql/statements",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": ws.slug},
            json={
                "statement": "SELECT i AS id, i % 10 AS bucket, i * 1.5 AS metric FROM range(0, 5000) t(i)",
                "row_limit": 5000,
                "chunk_size": 1000,
                "format": "JSON_ARRAY",
            },
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"]["state"] == "SUCCEEDED"
        assert body["manifest"]["total_row_count"] == 5000
        assert body["manifest"]["total_chunk_count"] == 5
        assert (
            elapsed_ms < 2500.0
        ), f"Expected 5,000-row chunked execution under 2.5s, took {elapsed_ms:.1f}ms"
