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

        allowed_embed, _ = is_s3_path_allowed(
            s3_uri="s3://datalake/embeddings/hubspot/data.parquet",
            workspace_slug="analytics",
            bucket="datalake",
        )
        assert allowed_embed is True

    def test_allowed_shared_catalog_pattern(self):
        allowed, _ = is_s3_path_allowed(
            s3_uri="s3://datalake/workspaces/shared_org/sales/2026/q1.parquet",
            workspace_slug="analytics",
            allowed_patterns=["s3://datalake/workspaces/shared_org/sales/**/*.parquet"],
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
