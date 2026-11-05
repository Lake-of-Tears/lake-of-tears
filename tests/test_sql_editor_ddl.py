"""Tests for role-aware DDL/DML statement execution in Lake SQL Editor (Databricks parity)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

ui_dir = str(Path(__file__).parents[1] / "ui")
backend_dir = str(Path(__file__).parents[1] / "backend")
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import main as ui_main  # noqa: E402


@pytest.fixture
def client():
    return TestClient(ui_main.app)


def _make_token(role: str = "user"):
    return jwt.encode(
        {"sub": f"user-{role}", "email": f"{role}@lake.local", "role": role},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )


class TestSqlEditorDdlExecution:
    def test_viewer_role_blocked_by_read_only_policy_on_create_table(self, client):
        token = _make_token(role="user")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        resp = client.post(
            "/sql",
            data={"sql": "CREATE TABLE bronze.sensor_data (id INT, val DOUBLE);"},
        )
        assert resp.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp.text
        assert "Only read-only queries" in resp.text

    def test_viewer_role_blocked_by_read_only_policy_on_drop_table(self, client):
        token = _make_token(role="user")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        resp = client.post(
            "/sql",
            data={"sql": "DROP TABLE bronze.sensor_data;"},
        )
        assert resp.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp.text

    def test_superadmin_can_execute_create_table(self, client):
        token = _make_token(role="superadmin")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        resp = client.post(
            "/sql",
            data={
                "sql": 'CREATE TABLE "bronze"."telemetry" (id VARCHAR NOT NULL, reading DOUBLE);'
            },
        )
        assert resp.status_code == 200
        assert "Query Blocked by Read-Only Policy" not in resp.text
        assert "Statement executed successfully" in resp.text

    def test_superadmin_can_execute_drop_table(self, client):
        token = _make_token(role="superadmin")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        # First create table
        client.post(
            "/sql",
            data={"sql": 'CREATE TABLE "bronze"."telemetry_to_drop" (id INT);'},
        )
        # Drop table
        resp = client.post(
            "/sql",
            data={"sql": 'DROP TABLE "bronze"."telemetry_to_drop";'},
        )
        assert resp.status_code == 200
        assert "Query Blocked by Read-Only Policy" not in resp.text
        assert "Statement executed successfully" in resp.text

    def test_host_filesystem_write_target_is_strictly_blocked_by_guardrails(self, client):
        token = _make_token(role="user")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        resp = client.post(
            "/sql",
            data={"sql": "COPY (SELECT 1) TO '/etc/passwd'"},
        )
        assert resp.status_code == 200
        assert "Query Blocked by Read-Only Policy" in resp.text

    def test_ui_api_execute_viewer_returns_read_only_error_info(self, client):
        token = _make_token(role="user")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        resp = client.post(
            "/api/ai/sql/execute",
            json={"sql": "CREATE TABLE bronze.metrics (id INT);"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "Only read-only queries" in data["error"]
        assert data["error_info"]["error_type"] == "READ_ONLY"

    def test_ui_api_execute_superadmin_allows_create_table(self, client):
        token = _make_token(role="superadmin")
        client.cookies.set("lake_token", token)
        client.cookies.set("lake_workspace_slug", "analytics")

        resp = client.post(
            "/api/ai/sql/execute",
            json={"sql": "CREATE TABLE bronze.kpi (id INT, score DOUBLE);"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["row_count"] == 0
