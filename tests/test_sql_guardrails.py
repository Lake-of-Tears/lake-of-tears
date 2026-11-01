"""Tests for DuckDB SQL Guardrail Engine for S3 Paths (Issue #253)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
ui_dir = str(Path(__file__).parents[1] / "ui")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

from sql_guardrails import (  # noqa: E402
    SQLGuardrailViolation,
    enforce_sql_s3_guardrails,
    extract_s3_patterns_from_catalogs,
    extract_s3_references,
    is_s3_path_allowed,
    normalize_s3_uri,
    s3_prefix_for_pattern,
    validate_sql_s3_paths,
)


class TestS3NormalizationAndPrefixes:
    def test_normalize_s3_uri_standard(self):
        uri = "s3://datalake/workspaces/marketing/sales/data.parquet"
        assert normalize_s3_uri(uri) == uri

    def test_normalize_s3_uri_directory_traversal(self):
        # Prevent traversal out of workspace jail
        uri = "s3://datalake/workspaces/marketing/../finance/secret.parquet"
        assert normalize_s3_uri(uri) == "s3://datalake/workspaces/finance/secret.parquet"

    def test_s3_prefix_for_pattern(self):
        pattern = "s3://datalake/workspaces/marketing/rev/**/*.parquet"
        assert s3_prefix_for_pattern(pattern, bucket="datalake") == "workspaces/marketing/rev/"

        single_file = "s3://datalake/workspaces/marketing/rev/data.parquet"
        assert s3_prefix_for_pattern(single_file, bucket="datalake") == "workspaces/marketing/rev/"

    def test_extract_s3_patterns_from_catalogs(self):
        catalogs = [
            {
                "name": "shared_cat",
                "schemas": [
                    {
                        "name": "gold",
                        "tables": [
                            {
                                "name": "orders",
                                "s3_path_pattern": "s3://datalake/workspaces/shared/orders/**/*.parquet",
                            },
                            {"name": "customers", "s3_path_pattern": ""},
                        ],
                    }
                ],
            }
        ]
        patterns = extract_s3_patterns_from_catalogs(catalogs)
        assert patterns == ["s3://datalake/workspaces/shared/orders/**/*.parquet"]


class TestASTExtraction:
    def test_extract_table_functions(self):
        sql = """
        SELECT * FROM read_parquet('s3://datalake/workspaces/ws1/a.parquet')
        JOIN scan_parquet('s3://datalake/workspaces/ws1/b.parquet') ON a.id = b.id
        LEFT JOIN read_csv('s3://datalake/workspaces/ws1/c.csv') ON a.id = c.id
        """
        refs = extract_s3_references(sql)
        assert refs == [
            "s3://datalake/workspaces/ws1/a.parquet",
            "s3://datalake/workspaces/ws1/b.parquet",
            "s3://datalake/workspaces/ws1/c.csv",
        ]

    def test_extract_direct_string_tables(self):
        sql = "SELECT * FROM 's3://datalake/workspaces/ws1/direct.parquet' LIMIT 10"
        refs = extract_s3_references(sql)
        assert refs == ["s3://datalake/workspaces/ws1/direct.parquet"]

    def test_extract_array_of_files(self):
        sql = "SELECT * FROM read_parquet(['s3://datalake/workspaces/ws1/1.parquet', 's3://datalake/workspaces/ws1/2.parquet'])"
        refs = extract_s3_references(sql)
        assert refs == [
            "s3://datalake/workspaces/ws1/1.parquet",
            "s3://datalake/workspaces/ws1/2.parquet",
        ]

    def test_extract_cte_and_subqueries(self):
        sql = """
        WITH cte AS (
            SELECT * FROM delta_scan('s3://datalake/workspaces/ws1/delta')
        )
        SELECT * FROM (
            SELECT * FROM glob('s3://datalake/workspaces/ws1/*.parquet')
        ) WHERE id IN (SELECT id FROM 's3://datalake/workspaces/ws1/sub.parquet')
        """
        refs = extract_s3_references(sql)
        assert refs == [
            "s3://datalake/workspaces/ws1/*.parquet",
            "s3://datalake/workspaces/ws1/delta",
            "s3://datalake/workspaces/ws1/sub.parquet",
        ]

    def test_no_s3_references(self):
        sql = "SELECT 1 AS num, 'hello' AS str"
        assert extract_s3_references(sql) == []

        view_sql = "SELECT * FROM marketing.campaigns WHERE status = 'active'"
        assert extract_s3_references(view_sql) == []


class TestPathAuthorization:
    def test_allowed_active_workspace_path(self):
        allowed, reason = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/analytics/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed is True
        assert reason is None

    def test_blocked_cross_workspace_path(self):
        allowed, reason = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/finance/payroll.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed is False
        assert "Access Denied" in reason
        assert "workspaces/analytics/" in reason
        assert "s3://datalake/workspaces/finance/payroll.parquet" in reason

    def test_blocked_prefix_collision(self):
        # 'analytics_extra' must NOT be allowed by 'analytics'
        allowed, reason = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/analytics_extra/secret.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed is False
        assert "Access Denied" in reason

    def test_blocked_foreign_bucket(self):
        allowed, reason = is_s3_path_allowed(
            s3_uri="s3://other-bucket/workspaces/analytics/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed is False
        assert "Only bucket 'datalake' is accessible" in reason

    def test_allowed_system_prefixes(self):
        allowed_raw, _ = is_s3_path_allowed(
            s3_uri="s3://datalake/raw/stripe_charges/2026/01/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed_raw is True

        # Global embeddings/ is deprecated from SYSTEM_ALLOWED_PREFIXES
        allowed_embed, err_embed = is_s3_path_allowed(
            s3_uri="s3://datalake/embeddings/hubspot/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed_embed is False
        assert "outside permitted prefix" in err_embed

        # Active workspace embeddings are permitted
        allowed_ws_embed, _ = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/analytics/embeddings/hubspot/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed_ws_embed is True

        # Other workspace embeddings are forbidden without shared catalog
        allowed_other_embed, err_other = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/marketing/embeddings/hubspot/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed_other_embed is False
        assert "outside permitted prefix" in err_other

    def test_allowed_shared_catalog_pattern(self):
        allowed, _ = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/shared_org/sales/2026/q1.parquet",
            workspace_slug="analytics",
            allowed_patterns=["s3://datalake/workspaces/shared_org/sales/**/*.parquet"],
            bucket="datalake",
        )
        assert allowed is True

    def test_shared_catalog_vector_index_permitted_by_guardrails(self):
        catalogs = [
            {
                "slug": "finance_cat",
                "schemas": [
                    {
                        "slug": "bronze",
                        "tables": [
                            {
                                "slug": "charges",
                                "s3_path_pattern": "s3://datalake/workspaces/finance/raw/charges/**/*.parquet",
                                "vector_index": {
                                    "enabled": True,
                                    "s3_vector_path": "s3://datalake/workspaces/finance/embeddings/charges/data.parquet",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        allowed, _ = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/finance/embeddings/charges/data.parquet",
            workspace_slug="analytics",
            allowed_patterns=extract_s3_patterns_from_catalogs(catalogs),
            bucket="datalake",
        )
        assert allowed is True


class TestValidateSqlS3Paths:
    def test_legitimate_query_succeeds(self):
        sql = (
            "SELECT * FROM read_parquet('s3://datalake/workspaces/marketing/data.parquet') LIMIT 10"
        )
        allowed, reason = validate_sql_s3_paths(
            sql=sql,
            workspace_slug="marketing",
            bucket="datalake",
        )
        assert allowed is True
        assert reason is None

    def test_out_of_boundary_query_fails(self):
        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/hr/salaries.parquet')"
        allowed, reason = validate_sql_s3_paths(
            sql=sql,
            workspace_slug="marketing",
            bucket="datalake",
        )
        assert allowed is False
        assert "Access Denied" in reason
        assert "workspaces/marketing/" in reason

    def test_directory_traversal_query_fails(self):
        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/marketing/../hr/salaries.parquet')"
        allowed, reason = validate_sql_s3_paths(
            sql=sql,
            workspace_slug="marketing",
            bucket="datalake",
        )
        assert allowed is False
        assert "Access Denied" in reason
        assert "s3://datalake/workspaces/hr/salaries.parquet" in reason

    def test_superadmin_bypasses_guardrail(self):
        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/hr/salaries.parquet')"
        allowed, reason = validate_sql_s3_paths(
            sql=sql,
            workspace_slug="marketing",
            is_superadmin=True,
            bucket="datalake",
        )
        assert allowed is True
        assert reason is None

    def test_fail_closed_on_unparseable_s3_query(self):
        # Malformed SQL that contains s3://
        sql = "SELECT FROM WHERE 's3://datalake/workspaces/other/bad.parquet' >>>"
        allowed, reason = validate_sql_s3_paths(
            sql=sql,
            workspace_slug="marketing",
            bucket="datalake",
        )
        assert allowed is False
        assert "Access Denied" in reason
        assert "failed syntax validation" in reason

    def test_fail_open_on_unparseable_non_s3_query(self):
        # Query with ordinary syntax error and no s3 references -> defer to DuckDB
        sql = "SELECT FROM WHERE 123 >>>"
        allowed, reason = validate_sql_s3_paths(
            sql=sql,
            workspace_slug="marketing",
            bucket="datalake",
        )
        assert allowed is True
        assert reason is None

    def test_enforce_raises_violation(self):
        sql = "SELECT * FROM 's3://datalake/workspaces/secret/data.parquet'"
        with pytest.raises(SQLGuardrailViolation) as exc_info:
            enforce_sql_s3_guardrails(sql=sql, workspace_slug="marketing", bucket="datalake")
        assert exc_info.value.status_code == 403
        assert "Access Denied" in str(exc_info.value)


class TestBackendExecutionGuardrails:
    def test_execute_sql_query_blocks_unauthorized_s3(self):
        import ai_service

        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/finance/ledger.parquet')"
        res = ai_service.execute_sql_query(
            sql=sql,
            workspace_slug="marketing",
            is_superadmin=False,
        )
        assert res["ok"] is False
        assert res.get("error_code") == 403
        assert "Access Denied" in res["error"]

    def test_execute_sql_query_allows_superadmin(self):
        import ai_service

        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/finance/ledger.parquet')"
        with patch.object(ai_service, "create_duckdb_connection") as mock_conn:
            mock_rel = MagicMock()
            mock_rel.description = [("id",)]
            mock_rel.fetchall.return_value = [(1,)]
            mock_conn.return_value.execute.return_value = mock_rel

            res = ai_service.execute_sql_query(
                sql=sql,
                workspace_slug="marketing",
                is_superadmin=True,
            )
            assert res["ok"] is True
            assert res["rows"] == [[1]]


class TestUIExecutionGuardrails:
    def test_ui_run_query_blocks_unauthorized_s3(self):
        import main as ui_main

        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/finance/ledger.parquet')"
        res = ui_main._run_query(
            sql=sql,
            workspace_slug="marketing",
            is_superadmin=False,
        )
        assert res["ok"] is False
        assert res.get("error_code") == 403
        assert "Access Denied" in res["error"]

    def test_ui_run_query_allows_in_boundary(self):
        import main as ui_main

        sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/marketing/data.parquet')"
        with patch.object(ui_main.duckdb_pool_instance, "acquire") as mock_acq:
            mock_con = MagicMock()
            mock_rel = MagicMock()
            mock_rel.description = [("a",)]
            mock_rel.fetchall.return_value = [(42,)]
            mock_con.execute.return_value = mock_rel
            mock_acq.return_value.__enter__.return_value = mock_con

            res = ui_main._run_query(
                sql=sql,
                workspace_slug="marketing",
                is_superadmin=False,
            )
            assert res["ok"] is True
            assert res["rows"] == [(42,)]


class TestFastAPIEndpoints:
    @pytest.fixture(autouse=True)
    def setup_apps(self):
        spec_backend = importlib.util.spec_from_file_location(
            "backend_main_guard", Path(__file__).parents[1] / "backend" / "main.py"
        )
        self.backend_main = importlib.util.module_from_spec(spec_backend)
        sys.modules["backend_main_guard"] = self.backend_main
        spec_backend.loader.exec_module(self.backend_main)
        self.backend_client = TestClient(self.backend_main.app)

        import main as ui_main

        self.ui_main = ui_main
        self.ui_client = TestClient(ui_main.app)

    def test_backend_api_execute_blocks_cross_workspace_403(self):
        import auth
        import database
        from models import User, Workspace, WorkspaceMember

        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email="dev@lake.local",
            display_name="Dev",
            role="user",
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
            name="Alpha Workspace",
            slug="ws-alpha",
            status="active",
        )
        member = WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=ws_id,
            user_id=user_id,
            role="read",
        )
        mock_db = MagicMock()

        def query_side_effect(model, *args, **kwargs):
            q = MagicMock()
            if model is Workspace:
                q.filter.return_value.first.return_value = workspace
                q.filter.return_value.order_by.return_value.first.return_value = workspace
            elif model is WorkspaceMember:
                q.filter.return_value.first.return_value = member
            else:
                q.filter.return_value.order_by.return_value.all.return_value = []
                q.filter.return_value.all.return_value = []
            return q

        mock_db.query.side_effect = query_side_effect
        self.backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
        self.backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

        try:
            resp = self.backend_client.post(
                "/api/v1/ai/sql/execute",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Workspace-Slug": workspace.slug,
                },
                json={
                    "sql": "SELECT * FROM read_parquet('s3://datalake/workspaces/other/data.parquet')"
                },
            )
            assert resp.status_code == 403
            assert "Access Denied" in resp.json()["detail"]
        finally:
            self.backend_main.app.dependency_overrides.clear()

    def test_ui_api_execute_blocks_cross_workspace_403(self):
        user_payload = {"sub": "user-123", "email": "user@lake.local", "role": "user"}
        token = jwt.encode(user_payload, self.ui_main.AUTH_SECRET_KEY, algorithm="HS256")
        self.ui_client.cookies.set("lake_token", token)
        self.ui_client.cookies.set("lake_workspace_slug", "marketing")

        with patch.object(self.ui_main, "_resolve_ui_catalogs", return_value=[]):
            resp = self.ui_client.post(
                "/api/ai/sql/execute",
                json={
                    "sql": "SELECT * FROM read_parquet('s3://datalake/workspaces/finance/secret.parquet')"
                },
            )
            assert resp.status_code == 403
            data = resp.json()
            assert data["ok"] is False
            assert "Access Denied" in data["error"]

    def test_is_read_only_sql_matrix(self):
        """Verify duckdb_ai and ai_service correctly classify read-only vs write statements (Issue #174)."""
        import ai_service
        import duckdb_ai

        valid_statements = [
            "SELECT 1",
            "SELECT * FROM tbl WHERE s = 'done;paid'",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "DESCRIBE tbl",
            "SUMMARIZE tbl",
            "SHOW TABLES",
            "EXPLAIN SELECT 1",
            "VALUES (1, 'val')",
            "(SELECT 1)",
        ]

        forbidden_statements = [
            "COPY (SELECT 1 AS x) TO 's3://datalake/test.parquet'",
            "COPY tbl FROM 's3://datalake/test.parquet'",
            "CREATE TABLE t AS SELECT 1",
            "CREATE OR REPLACE TABLE t AS SELECT 1",
            "DROP TABLE t",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET col = 1",
            "DELETE FROM t",
            "ALTER TABLE t ADD COLUMN c INT",
            "ATTACH 'test.db' AS test_db",
            "DETACH test_db",
            "INSTALL httpfs",
            "LOAD httpfs",
            "PRAGMA threads=4",
            "SELECT 1; SELECT 2",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        ]

        for mod in (duckdb_ai, ai_service):
            for sql in valid_statements:
                ok, err = mod.is_read_only_sql(sql)
                assert ok is True, f"Expected {sql} to be allowed by {mod.__name__}, got err: {err}"

            for sql in forbidden_statements:
                ok, err = mod.is_read_only_sql(sql)
                assert ok is False, f"Expected {sql} to be rejected by {mod.__name__}"
                assert err is not None

    def test_sql_editor_post_read_only_enforcement_for_all_roles(self):
        """Verify POST /sql in UI blocks write statements for both standard users and superadmins (Issue #174)."""
        # 1. Standard user / viewer attempts write queries
        user_payload = {"sub": "user-std", "email": "viewer@lake.local", "role": "user"}
        token = jwt.encode(user_payload, self.ui_main.AUTH_SECRET_KEY, algorithm="HS256")
        self.ui_client.cookies.set("lake_token", token)
        self.ui_client.cookies.set("lake_workspace_slug", "default")

        resp_copy = self.ui_client.post(
            "/sql",
            data={"sql": "COPY (SELECT 1 AS x) TO 's3://datalake/test.parquet'"},
        )
        assert resp_copy.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp_copy.text
        assert "Only read-only queries" in resp_copy.text

        resp_create = self.ui_client.post(
            "/sql",
            data={"sql": "CREATE TABLE t_evil AS SELECT 1"},
        )
        assert resp_create.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp_create.text

        # 2. Superadmin also has read-only policy enforced on POST /sql
        admin_payload = {"sub": "admin-1", "email": "admin@lake.local", "role": "superadmin"}
        admin_token = jwt.encode(admin_payload, self.ui_main.AUTH_SECRET_KEY, algorithm="HS256")
        self.ui_client.cookies.set("lake_token", admin_token)

        resp_admin_copy = self.ui_client.post(
            "/sql",
            data={"sql": "COPY (SELECT 1 AS x) TO 's3://datalake/admin_test.parquet'"},
        )
        assert resp_admin_copy.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp_admin_copy.text

        resp_admin_create = self.ui_client.post(
            "/sql",
            data={"sql": "CREATE TABLE t_admin AS SELECT 1"},
        )
        assert resp_admin_create.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp_admin_create.text

        # 3. Read query succeeds
        resp_read = self.ui_client.post(
            "/sql",
            data={"sql": "SELECT 98765 AS test_metric"},
        )
        assert resp_read.status_code == 200
        assert "98765" in resp_read.text
        assert "Query Blocked by Read-Only Policy" not in resp_read.text

    def test_api_sql_execute_unconditionally_enforces_read_only(self):
        """Verify UI and backend SQL execution endpoints block writes even if read_only=False is passed (Issue #174)."""
        import auth
        import database
        from models import User, Workspace, WorkspaceMember

        # 1. UI endpoint /api/ai/sql/execute
        user_payload = {"sub": "user-std", "email": "viewer@lake.local", "role": "user"}
        token = jwt.encode(user_payload, self.ui_main.AUTH_SECRET_KEY, algorithm="HS256")
        self.ui_client.cookies.set("lake_token", token)
        self.ui_client.cookies.set("lake_workspace_slug", "default")

        resp_ui = self.ui_client.post(
            "/api/ai/sql/execute",
            json={
                "sql": "COPY (SELECT 1 AS x) TO 's3://datalake/test.parquet'",
                "read_only": False,
            },
        )
        assert resp_ui.status_code == 200
        data_ui = resp_ui.json()
        assert data_ui["ok"] is False
        assert "Only read-only queries" in data_ui["error"]
        assert data_ui["error_info"]["error_type"] == "READ_ONLY"

        # 2. Backend endpoint /api/v1/ai/sql/execute
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email="dev@lake.local",
            display_name="Dev",
            role="user",
            is_active=True,
        )
        ws_id = uuid.uuid4()
        workspace = Workspace(
            id=ws_id,
            name="Alpha Workspace",
            slug="ws-alpha",
            status="active",
        )
        member = WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=ws_id,
            user_id=user_id,
            role="read",
        )
        mock_db = MagicMock()

        def query_side_effect(model, *args, **kwargs):
            q = MagicMock()
            if model is Workspace:
                q.filter.return_value.first.return_value = workspace
                q.filter.return_value.order_by.return_value.first.return_value = workspace
            elif model is WorkspaceMember:
                q.filter.return_value.first.return_value = member
            else:
                q.filter.return_value.order_by.return_value.all.return_value = []
                q.filter.return_value.all.return_value = []
            return q

        mock_db.query.side_effect = query_side_effect
        self.backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
        self.backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

        auth_token = jwt.encode(
            {"sub": str(user_id), "email": user.email, "role": user.role},
            auth.SECRET_KEY,
            algorithm="HS256",
        )
        try:
            resp_be = self.backend_client.post(
                "/api/v1/ai/sql/execute",
                headers={
                    "Authorization": f"Bearer {auth_token}",
                    "X-Workspace-Slug": workspace.slug,
                },
                json={"sql": "CREATE TABLE t AS SELECT 1", "read_only": False},
            )
            assert resp_be.status_code == 200
            data_be = resp_be.json()
            assert data_be["ok"] is False
            assert "Only read-only queries" in data_be["error"]
        finally:
            self.backend_main.app.dependency_overrides.clear()
