from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

client = TestClient(main.app)


def _create_auth_cookie(role: str = "member", global_role: str = "user") -> tuple[str, list[dict]]:
    token = jwt.encode(
        {"sub": f"{role}@lake.local", "email": f"{role}@lake.local", "role": global_role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": role}]
    return token, workspaces


# ── UI Page Rendering Tests ───────────────────────────────────────────────────


def test_get_pipelines_page_renders_native_template():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/pipelines",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify native UI, no iframe or embed.html
        assert "<iframe" not in response.text
        assert "embed.html" not in response.text

        # Verify key pipeline UI elements
        assert 'id="pipelines-container"' in response.text
        assert 'id="pipeline-search"' in response.text
        assert 'id="filter-all"' in response.text
        assert 'id="filter-active"' in response.text
        assert 'id="filter-paused"' in response.text
        assert 'id="filter-running"' in response.text
        assert 'id="filter-failed"' in response.text
        assert 'id="inspector-drawer"' in response.text
        assert 'id="trigger-modal"' in response.text
        assert "Schedule Notebook" in response.text

        # Verify pipelines pagination elements (#239)
        assert 'id="pipelines-pagination-toolbar"' in response.text
        assert 'id="pipelines-pagination-info"' in response.text
        assert 'id="pipelines-page-size"' in response.text
        assert 'id="btn-pipelines-prev"' in response.text
        assert 'id="btn-pipelines-next"' in response.text
        assert 'id="pipelines-page-numbers"' in response.text
        assert '<option value="10">10</option>' in response.text
        assert '<option value="25">25</option>' in response.text
        assert '<option value="50">50</option>' in response.text


def test_get_pipelines_page_viewer_mode_restrictions():
    token, mock_workspaces = _create_auth_cookie(role="viewer")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/pipelines",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert "Read-Only Viewer" in response.text
        # Viewer should not have "+ Schedule Notebook" header button
        assert "Schedule Notebook" not in response.text


def test_notebooks_page_contains_enhanced_schedule_modal():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/notebooks",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify the two tabs in #airflow-modal
        assert 'id="airflow-modal"' in response.text
        assert 'id="tab-run-now"' in response.text
        assert 'id="tab-schedule-recurring"' in response.text

        # Verify recurring schedule inputs
        assert 'id="airflow-sched-title"' in response.text
        assert 'id="airflow-sched-cadence"' in response.text
        assert 'id="wrap-custom-cron"' in response.text
        assert 'id="airflow-sched-cron"' in response.text
        assert 'id="airflow-sched-params"' in response.text
        assert "Open Pipelines Dashboard" in response.text


# ── UI Proxy Endpoints Tests ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ui_pipeline_proxy_list():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = httpx.Response(
        200,
        json={"workspace": "analytics", "total": 1, "pipelines": [{"dag_id": "ws_analytics_etl"}]},
        request=httpx.Request("GET", "http://backend:8000/api/v1/pipelines"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get,
    ):
        res = client.get(
            "/api/pipelines",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["workspace"] == "analytics"
        assert len(data["pipelines"]) == 1
        assert mock_get.called


@pytest.mark.anyio
async def test_ui_pipeline_proxy_trigger():
    token, mock_workspaces = _create_auth_cookie(role="editor")

    mock_resp = httpx.Response(
        201,
        json={"dag_id": "ws_analytics_etl", "dag_run_id": "manual__1", "state": "queued"},
        request=httpx.Request(
            "POST", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/trigger"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post,
    ):
        res = client.post(
            "/api/pipelines/ws_analytics_etl/trigger",
            json={"conf": {"batch": 1}},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 201
        assert res.json()["dag_run_id"] == "manual__1"
        assert mock_post.called


@pytest.mark.anyio
async def test_ui_pipeline_proxy_schedule_notebook():
    token, mock_workspaces = _create_auth_cookie(role="editor")

    mock_resp = httpx.Response(
        201,
        json={"dag_id": "ws_analytics_forecast", "status": "created"},
        request=httpx.Request("POST", "http://backend:8000/api/v1/pipelines/schedule-notebook"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post,
    ):
        res = client.post(
            "/api/pipelines/schedule-notebook",
            json={
                "notebook_path": "forecast.ipynb",
                "schedule_interval": "@daily",
                "pipeline_title": "Daily Forecast",
            },
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 201
        assert res.json()["dag_id"] == "ws_analytics_forecast"
        assert mock_post.called


@pytest.mark.anyio
async def test_ui_pipeline_proxy_delete_schedule():
    token, mock_workspaces = _create_auth_cookie(role="editor")

    mock_resp = httpx.Response(
        200,
        json={"dag_id": "ws_analytics_forecast", "status": "deleted"},
        request=httpx.Request(
            "DELETE", "http://backend:8000/api/v1/pipelines/schedule-notebook/ws_analytics_forecast"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.delete", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_del,
    ):
        res = client.delete(
            "/api/pipelines/schedule-notebook/ws_analytics_forecast",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "deleted"
        assert mock_del.called


@pytest.mark.anyio
async def test_ui_pipeline_proxy_runs_tasks_logs():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_runs_resp = httpx.Response(
        200,
        json={"dag_id": "ws_analytics_etl", "runs": [{"dag_run_id": "r1", "state": "success"}]},
        request=httpx.Request("GET", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/runs"),
    )
    mock_tasks_resp = httpx.Response(
        200,
        json={
            "dag_id": "ws_analytics_etl",
            "run_id": "r1",
            "tasks": [{"task_id": "t1", "state": "success"}],
        },
        request=httpx.Request(
            "GET", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/runs/r1/tasks"
        ),
    )
    mock_logs_resp = httpx.Response(
        200,
        json={
            "dag_id": "ws_analytics_etl",
            "run_id": "r1",
            "task_id": "t1",
            "content": "Sample log line",
        },
        request=httpx.Request(
            "GET", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/runs/r1/tasks/t1/logs"
        ),
    )

    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        # 1. Runs
        mock_get.return_value = mock_runs_resp
        r_runs = client.get("/api/pipelines/ws_analytics_etl/runs", cookies=cookies)
        assert r_runs.status_code == 200
        assert len(r_runs.json()["runs"]) == 1

        # 2. Tasks
        mock_get.return_value = mock_tasks_resp
        r_tasks = client.get("/api/pipelines/ws_analytics_etl/runs/r1/tasks", cookies=cookies)
        assert r_tasks.status_code == 200
        assert len(r_tasks.json()["tasks"]) == 1

        # 3. Logs
        mock_get.return_value = mock_logs_resp
        r_logs = client.get(
            "/api/pipelines/ws_analytics_etl/runs/r1/tasks/t1/logs", cookies=cookies
        )
        assert r_logs.status_code == 200
        assert "Sample log line" in r_logs.json()["content"]


def test_get_pipelines_page_renders_timeline_container_and_controls():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/pipelines",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify 24-hour visual schedule timeline elements
        assert 'id="timeline-banner"' in response.text
        assert 'id="timeline-date-nav"' in response.text
        assert 'id="btn-timeline-today"' in response.text
        assert 'id="timeline-datepicker"' in response.text
        assert 'id="timeline-current-date-label"' in response.text
        assert 'id="timeline-zoom-controls"' in response.text
        assert 'id="btn-zoom-24h"' in response.text
        assert 'id="btn-zoom-12h"' in response.text
        assert 'id="btn-zoom-6h"' in response.text
        assert 'id="btn-zoom-1h"' in response.text
        assert 'id="timeline-viewport"' in response.text
        assert 'id="timeline-canvas"' in response.text
        assert 'id="timeline-ruler"' in response.text
        assert 'id="timeline-grid-overlay"' in response.text
        assert 'id="timeline-swimlanes"' in response.text
        assert 'id="color-picker-popover"' in response.text
        assert 'id="btn-open-calendar"' in response.text
        assert 'id="calendar-popover"' in response.text
        assert 'id="calendar-month-year-label"' in response.text
        assert 'id="calendar-days-grid"' in response.text
        assert 'html[data-theme="light"]' in response.text

        # Verify mouse wheel zoom and cursor anchoring script definitions
        assert "const ZOOM_LEVELS = ['24h', '12h', '6h', '1h'];" in response.text
        assert "function setTimelineZoom(level, anchorClientX = null)" in response.text
        assert "function stepTimelineZoom(zoomIn, anchorClientX = null)" in response.text
        assert "WHEEL_ZOOM_THRESHOLD = 50" in response.text
        assert "overscroll-behavior: contain" in response.text


@pytest.mark.anyio
async def test_ui_timeline_proxy():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = httpx.Response(
        200,
        json={
            "date": "2026-10-14",
            "timezone": "UTC",
            "window_start": "2026-10-14T00:00:00+00:00",
            "window_end": "2026-10-14T23:59:59.999999+00:00",
            "pipelines": [],
        },
        request=httpx.Request("GET", "http://backend:8000/api/v1/pipelines/timeline"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get,
    ):
        res = client.get(
            "/api/pipelines/timeline?date=2026-10-14&tz=America/New_York",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["date"] == "2026-10-14"
        assert mock_get.called


@pytest.mark.anyio
async def test_ui_pipeline_settings_proxy():
    token, mock_workspaces = _create_auth_cookie(role="editor")

    mock_get_resp = httpx.Response(
        200,
        json={"dag_id": "ws_analytics_etl", "color_hex": "#3B82F6", "palette": ["#3B82F6"]},
        request=httpx.Request(
            "GET", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/settings"
        ),
    )
    mock_patch_resp = httpx.Response(
        200,
        json={"dag_id": "ws_analytics_etl", "color_hex": "#10B981"},
        request=httpx.Request(
            "PATCH", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/settings"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_get_resp
        ) as mock_get,
        patch(
            "httpx.AsyncClient.patch", new_callable=AsyncMock, return_value=mock_patch_resp
        ) as mock_patch,
    ):
        cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

        # 1. GET settings
        r_get = client.get("/api/pipelines/ws_analytics_etl/settings", cookies=cookies)
        assert r_get.status_code == 200
        assert r_get.json()["color_hex"] == "#3B82F6"
        assert mock_get.called

        # 2. PATCH settings
        r_patch = client.patch(
            "/api/pipelines/ws_analytics_etl/settings",
            json={"color_hex": "#10B981"},
            cookies=cookies,
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["color_hex"] == "#10B981"
        assert mock_patch.called


@pytest.mark.anyio
async def test_ui_cancel_pipeline_run_proxy():
    token, mock_workspaces = _create_auth_cookie(role="editor")

    mock_resp = httpx.Response(
        200,
        json={
            "dag_id": "ws_analytics_etl",
            "dag_run_id": "manual__1",
            "state": "failed",
            "message": "DAG run cancelled successfully",
        },
        request=httpx.Request(
            "POST", "http://backend:8000/api/v1/pipelines/ws_analytics_etl/runs/manual__1/cancel"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post,
    ):
        cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}
        res = client.post("/api/pipelines/ws_analytics_etl/runs/manual__1/cancel", cookies=cookies)
        assert res.status_code == 200
        assert res.json()["state"] == "failed"
        assert mock_post.called


def test_ui_workspace_search_finds_live_pipelines():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_backend_pipeline_data = {
        "workspace": "analytics",
        "total": 2,
        "pipelines": [
            {
                "dag_id": "ws_analytics_anomaly_detect",
                "display_label": "Anomaly Detection Pipeline",
                "description": "Scans sales data for anomalies",
            },
            {
                "dag_id": "ws_analytics_nightly_backup",
                "display_label": "Nightly Backup",
                "description": "MinIO backup job",
            },
        ],
    }

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=mock_backend_pipeline_data),
    ):
        cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}
        res = client.get("/api/workspace/search?q=anomaly", cookies=cookies)
        assert res.status_code == 200
        data = res.json()
        results = data.get("results", [])
        pipeline_results = [r for r in results if r.get("category") == "Pipelines"]
        assert len(pipeline_results) == 1
        assert pipeline_results[0]["title"] == "Anomaly Detection Pipeline"
        assert pipeline_results[0]["type"] == "pipeline"
        assert pipeline_results[0]["dag_id"] == "ws_analytics_anomaly_detect"
        assert pipeline_results[0]["url"] == "/pipelines?open=ws_analytics_anomaly_detect"


def test_pipelines_page_renders_editable_fields_and_notebook_link():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/pipelines",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert 'id="drawer-name-input"' in response.text
        assert 'id="drawer-desc-input"' in response.text
        assert 'id="drawer-notebook-link-wrap"' in response.text
        assert 'id="drawer-active-run-alert"' in response.text
        assert 'id="drawer-metadata-section"' in response.text
        assert 'id="drawer-color-swatch"' in response.text


def test_pipelines_page_includes_highlight_and_row_ids():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/pipelines",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert "highlightPipelineRow" in response.text
        assert "pipeline-row-" in response.text
        assert "openDagId" in response.text


def test_notebooks_page_includes_pipeline_redirect_and_prepopulation():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/notebooks",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert "loadExistingPipelineForNotebook" in response.text
        assert "/pipelines?open=" in response.text
        assert "airflow-sched-cadence" in response.text
