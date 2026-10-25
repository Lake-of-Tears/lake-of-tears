from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

# Ensure backend directory is in sys.path
backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import pipeline_service  # noqa: E402
from models import User, Workspace, WorkspaceMember  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_pipelines", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_pipelines"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


# ── Helpers & Fixtures ────────────────────────────────────────────────────────


def _generate_test_user_and_token(role: str = "member", global_role: str = "user"):
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}@lake.local",
        display_name=role.title(),
        role=global_role,
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
        name="Analytics Workspace",
        slug="analytics",
        status="active",
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    return user, workspace, member, token


# ── Service Layer Unit Tests: Sanitization & Helpers ─────────────────────────


def test_sanitize_dag_id_valid():
    assert pipeline_service.sanitize_dag_id("ws_analytics_daily_etl") == "ws_analytics_daily_etl"
    assert pipeline_service.sanitize_dag_id("pipeline-job-123") == "pipeline-job-123"


def test_sanitize_dag_id_traversal_blocked():
    with pytest.raises(ValueError, match="Invalid path traversal"):
        pipeline_service.sanitize_dag_id("../secret_dag")
    with pytest.raises(ValueError, match="Invalid path traversal"):
        pipeline_service.sanitize_dag_id("sub/../../etc")
    with pytest.raises(ValueError, match="cannot be empty"):
        pipeline_service.sanitize_dag_id("   ")


def test_sanitize_pipeline_name_valid():
    assert pipeline_service.sanitize_pipeline_name("Daily Analytics ETL") == "Daily Analytics ETL"
    assert pipeline_service.sanitize_pipeline_name("Job-123_test") == "Job-123_test"


def test_sanitize_pipeline_name_traversal_blocked():
    with pytest.raises(ValueError, match="Invalid path traversal"):
        pipeline_service.sanitize_pipeline_name("../../bad_path")
    with pytest.raises(ValueError, match="cannot be empty"):
        pipeline_service.sanitize_pipeline_name("")


def test_resolve_workspace_dag_id():
    assert (
        pipeline_service.resolve_workspace_dag_id("analytics", "Daily ETL")
        == "ws_analytics_daily_etl"
    )
    assert (
        pipeline_service.resolve_workspace_dag_id("finance", "quarterly-report!")
        == "ws_finance_quarterly-report"
    )


def test_is_dag_accessible_by_workspace():
    # Tag-based matching
    dag_tagged = {
        "dag_id": "custom_etl",
        "tags": [{"name": "workspace:analytics"}, {"name": "scheduled"}],
    }
    assert pipeline_service.is_dag_accessible_by_workspace(dag_tagged, "analytics")
    assert not pipeline_service.is_dag_accessible_by_workspace(dag_tagged, "finance")

    # Prefix-based matching
    dag_prefixed = {
        "dag_id": "ws_analytics_weekly_metrics",
        "tags": [],
    }
    assert pipeline_service.is_dag_accessible_by_workspace(dag_prefixed, "analytics")
    assert not pipeline_service.is_dag_accessible_by_workspace(dag_prefixed, "finance")

    # Superadmin scope jailing (#249): superadmin cannot bypass active workspace scope
    assert not pipeline_service.is_dag_accessible_by_workspace(
        dag_tagged, "finance", is_superadmin=True
    )

    # Global run_notebook is an internal runner and strictly excluded from workspace DAG lists (#256)
    assert not pipeline_service.is_dag_accessible_by_workspace(
        {"dag_id": "run_notebook", "tags": []}, "finance"
    )
    assert not pipeline_service.is_dag_accessible_by_workspace(
        {"dag_id": "run_notebook", "tags": []}, "default"
    )


def test_calculate_duration():
    assert pipeline_service._calculate_duration(None, None, None) is None
    dur = pipeline_service._calculate_duration(
        "2026-10-14T10:00:00Z", "2026-10-14T10:02:30Z", "success"
    )
    assert dur == 150.0


def test_airflow_url_and_auth_resolution():
    with patch.dict(
        os.environ,
        {"AIRFLOW_URL": "http://test-airflow:8080/airflow", "AIRFLOW_PASSWORD": "secret_pass"},
    ):
        assert pipeline_service.get_airflow_api_base() == "http://test-airflow:8080/airflow/api/v1"
        auth_creds = pipeline_service.get_airflow_auth()
        assert auth_creds == ("admin", "secret_pass")


# ── Dynamic DAG Code Generation & AST Validation ─────────────────────────────


def test_dynamic_dag_generator_ast_syntax_validity():
    # Docker mode
    code_docker = pipeline_service.generate_dynamic_dag_code(
        workspace_slug="analytics",
        dag_id="ws_analytics_daily_forecast",
        notebook_path="models/forecast.ipynb",
        schedule_interval="0 4 * * *",
        pipeline_title="Daily Sales Forecast",
        parameters={"alpha": 0.05},
        mode="docker",
    )
    parsed_docker = ast.parse(code_docker)
    assert parsed_docker is not None
    assert "ws_analytics_daily_forecast" in code_docker
    assert "PythonOperator" in code_docker

    # Kubernetes mode
    code_k8s = pipeline_service.generate_dynamic_dag_code(
        workspace_slug="analytics",
        dag_id="ws_analytics_heavy_etl",
        notebook_path="etl/large.ipynb",
        schedule_interval="@daily",
        pipeline_title="Large Scale ETL",
        parameters={"partitions": 32},
        compute_resources={"cpu": "1", "memory": "2Gi"},
        mode="k8s",
    )
    parsed_k8s = ast.parse(code_k8s)
    assert parsed_k8s is not None
    assert "KubernetesPodOperator" in code_k8s
    assert 'requests={"cpu": "1", "memory": "2Gi"}' in code_k8s


def test_schedule_notebook_and_delete():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(pipeline_service, "get_airflow_dags_dir", return_value=Path(tmpdir)):
            # 1. Schedule notebook DAG
            res = pytest.importorskip("asyncio").run(
                pipeline_service.schedule_notebook(
                    workspace_slug="marketing",
                    notebook_path="campaigns/october.ipynb",
                    schedule_interval="0 0 * * 1",
                    pipeline_title="Weekly Campaign Metrics",
                )
            )
            assert res["status"] == "created"
            assert res["dag_id"] == "ws_marketing_weekly_campaign_metrics"

            dag_file = Path(res["file_path"])
            assert dag_file.exists()
            assert "ws_marketing_weekly_campaign_metrics" in dag_file.read_text()

            # 2. Delete dynamic DAG
            del_res = pytest.importorskip("asyncio").run(
                pipeline_service.delete_scheduled_notebook(
                    workspace_slug="marketing",
                    dag_id=res["dag_id"],
                )
            )
            assert del_res["status"] == "deleted"
            assert not dag_file.exists()


# ── Backend REST Endpoints Tests ──────────────────────────────────────────────


def test_pipelines_unauthenticated_blocked():
    backend_main.app.dependency_overrides.clear()
    res = client.get("/api/v1/pipelines")
    assert res.status_code == 401
    assert "Not authenticated" in res.text


def test_pipelines_viewer_can_read_all():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "viewer"),
            ),
            patch.object(
                pipeline_service,
                "list_workspace_dags",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "dag_id": "ws_analytics_daily_etl",
                        "pipeline_name": "Daily Analytics ETL",
                        "description": "Daily metrics run",
                        "is_paused": False,
                        "schedule_interval": "@daily",
                        "tags": ["workspace:analytics"],
                        "last_run": {"state": "success", "duration_seconds": 120.0},
                    }
                ],
            ),
            patch.object(
                pipeline_service,
                "get_dag_details",
                new_callable=AsyncMock,
                return_value={
                    "dag_id": "ws_analytics_daily_etl",
                    "pipeline_name": "Daily Analytics ETL",
                    "is_paused": False,
                    "schedule_interval": "@daily",
                    "tags": ["workspace:analytics"],
                },
            ),
            patch.object(
                pipeline_service,
                "list_dag_runs",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "dag_run_id": "scheduled__2026-10-14T00:00:00Z",
                        "state": "success",
                        "duration_seconds": 120.0,
                    }
                ],
            ),
            patch.object(
                pipeline_service,
                "get_task_log",
                new_callable=AsyncMock,
                return_value={
                    "dag_id": "ws_analytics_daily_etl",
                    "run_id": "run_1",
                    "task_id": "execute",
                    "try_number": 1,
                    "content": "Execution log line 1\nExecution log line 2",
                },
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # 1. List pipelines
            res = client.get("/api/v1/pipelines", headers=headers)
            assert res.status_code == 200
            assert res.json()["workspace"] == "analytics"
            assert len(res.json()["pipelines"]) == 1

            # 2. Get single pipeline
            res_single = client.get("/api/v1/pipelines/ws_analytics_daily_etl", headers=headers)
            assert res_single.status_code == 200
            assert res_single.json()["dag_id"] == "ws_analytics_daily_etl"

            # 3. Get runs
            res_runs = client.get("/api/v1/pipelines/ws_analytics_daily_etl/runs", headers=headers)
            assert res_runs.status_code == 200
            assert len(res_runs.json()["runs"]) == 1

            # 4. Get logs
            res_logs = client.get(
                "/api/v1/pipelines/ws_analytics_daily_etl/runs/run_1/tasks/execute/logs",
                headers=headers,
            )
            assert res_logs.status_code == 200
            assert "Execution log line 1" in res_logs.json()["content"]

            # 5. Get runs using cookie lake_workspace_id without X-Workspace-Id header
            cookie_headers = {"Authorization": f"Bearer {token}"}
            res_cookie_runs = client.get(
                "/api/v1/pipelines/ws_analytics_daily_etl/runs",
                headers=cookie_headers,
                cookies={"lake_workspace_id": str(workspace.id)},
            )
            assert res_cookie_runs.status_code == 200
            assert len(res_cookie_runs.json()["runs"]) == 1
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipelines_viewer_mutation_forbidden():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with patch(
            "pipelines_router.get_pipeline_workspace_and_role",
            side_effect=backend_main.HTTPException(
                403, "Workspace viewer role is read-only. Cannot modify or trigger pipelines."
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Pause pipeline -> 403
            r_pause = client.patch(
                "/api/v1/pipelines/ws_analytics_daily_etl",
                json={"is_paused": True},
                headers=headers,
            )
            assert r_pause.status_code == 403
            assert "Workspace viewer role is read-only" in r_pause.text

            # 2. Trigger pipeline -> 403
            r_trigger = client.post(
                "/api/v1/pipelines/ws_analytics_daily_etl/trigger",
                json={"conf": {}},
                headers=headers,
            )
            assert r_trigger.status_code == 403

            # 3. Schedule notebook -> 403
            r_sched = client.post(
                "/api/v1/pipelines/schedule-notebook",
                json={"notebook_path": "a.ipynb", "schedule_interval": "@daily"},
                headers=headers,
            )
            assert r_sched.status_code == 403

            # 4. Delete schedule -> 403
            r_del = client.delete(
                "/api/v1/pipelines/schedule-notebook/ws_analytics_daily_etl",
                headers=headers,
            )
            assert r_del.status_code == 403

            # 5. Delete pipeline -> 403
            r_del2 = client.delete(
                "/api/v1/pipelines/ws_analytics_daily_etl",
                headers=headers,
            )
            assert r_del2.status_code == 403
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipelines_editor_and_admin_can_mutate():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "editor"),
            ),
            patch.object(
                pipeline_service,
                "set_dag_paused",
                new_callable=AsyncMock,
                return_value={"dag_id": "ws_analytics_daily_etl", "is_paused": True},
            ),
            patch.object(
                pipeline_service,
                "trigger_dag_run",
                new_callable=AsyncMock,
                return_value={
                    "dag_id": "ws_analytics_daily_etl",
                    "dag_run_id": "manual__1",
                    "state": "queued",
                },
            ),
            patch.object(
                pipeline_service,
                "schedule_notebook",
                new_callable=AsyncMock,
                return_value={"dag_id": "ws_analytics_test", "status": "created"},
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Pause
            r_pause = client.patch(
                "/api/v1/pipelines/ws_analytics_daily_etl",
                json={"is_paused": True},
                headers=headers,
            )
            assert r_pause.status_code == 200
            assert r_pause.json()["is_paused"] is True

            # 2. Trigger
            r_trigger = client.post(
                "/api/v1/pipelines/ws_analytics_daily_etl/trigger",
                json={"conf": {"mode": "incremental"}},
                headers=headers,
            )
            assert r_trigger.status_code == 201
            assert r_trigger.json()["dag_run_id"] == "manual__1"

            # 3. Schedule notebook
            r_sched = client.post(
                "/api/v1/pipelines/schedule-notebook",
                json={"notebook_path": "test.ipynb", "schedule_interval": "@hourly"},
                headers=headers,
            )
            assert r_sched.status_code == 201
            assert r_sched.json()["status"] == "created"

            # 4. Delete pipeline
            with patch.object(
                pipeline_service,
                "delete_scheduled_notebook",
                new_callable=AsyncMock,
                return_value={"dag_id": "ws_analytics_daily_etl", "status": "deleted"},
            ) as mock_delete:
                r_del = client.delete(
                    "/api/v1/pipelines/ws_analytics_daily_etl",
                    headers=headers,
                )
                assert r_del.status_code == 200
                assert r_del.json()["status"] == "deleted"
                assert mock_delete.called
    finally:
        backend_main.app.dependency_overrides.clear()


def test_delete_pipeline_protection_and_rules():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with patch(
            "pipelines_router.get_pipeline_workspace_and_role",
            return_value=(workspace, "editor"),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Reject protected system DAGs
            res_system = client.delete("/api/v1/pipelines/run_notebook", headers=headers)
            assert res_system.status_code == 400
            assert (
                "Cannot delete protected or non-workspace pipeline" in res_system.json()["detail"]
            )

            res_ingest = client.delete("/api/v1/pipelines/ingest_weather_dag", headers=headers)
            assert res_ingest.status_code == 400
            assert (
                "Cannot delete protected or non-workspace pipeline" in res_ingest.json()["detail"]
            )

            # 2. Reject non-workspace pipeline
            res_non_ws = client.delete("/api/v1/pipelines/other_dag", headers=headers)
            assert res_non_ws.status_code == 400
            assert (
                "Cannot delete protected or non-workspace pipeline" in res_non_ws.json()["detail"]
            )

            # 3. Reject DAG from another workspace (404)
            res_foreign = client.delete("/api/v1/pipelines/ws_marketing_etl", headers=headers)
            assert res_foreign.status_code == 404
            assert "not found in this workspace" in res_foreign.json()["detail"]

            # 4. Valid workspace DAG deletion succeeds
            with patch.object(
                pipeline_service,
                "delete_scheduled_notebook",
                new_callable=AsyncMock,
                return_value={"dag_id": "ws_analytics_my_job", "status": "deleted"},
            ) as mock_delete:
                res_ok = client.delete("/api/v1/pipelines/ws_analytics_my_job", headers=headers)
                assert res_ok.status_code == 200
                assert res_ok.json() == {"dag_id": "ws_analytics_my_job", "status": "deleted"}
                assert mock_delete.called
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipelines_workspace_tenant_isolation():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "editor"),
            ),
            patch.object(
                pipeline_service,
                "get_dag_details",
                side_effect=backend_main.HTTPException(404, "Pipeline ws_finance_secret not found"),
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}
            # Attempt to access another workspace's pipeline
            res = client.get("/api/v1/pipelines/ws_finance_secret", headers=headers)
            assert res.status_code == 404
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipelines_airflow_unreachable_error_handling():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "viewer"),
            ),
            patch.object(
                pipeline_service,
                "list_workspace_dags",
                side_effect=backend_main.HTTPException(
                    503, "Airflow service unavailable: connect timeout"
                ),
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}
            res = client.get("/api/v1/pipelines", headers=headers)
            assert res.status_code == 503
            assert "Airflow service unavailable" in res.text
    finally:
        backend_main.app.dependency_overrides.clear()


# ── Timeline & Pipeline Color Settings Tests ─────────────────────────────────


def test_get_default_color_for_dag():
    color1 = pipeline_service.get_default_color_for_dag("ws_analytics_etl")
    color2 = pipeline_service.get_default_color_for_dag("ws_analytics_etl")
    assert color1 == color2
    assert color1 in pipeline_service.DEFAULT_PALETTE
    assert color1.startswith("#")


def test_compute_projected_runs_for_day():
    start = datetime(2026, 10, 14, 0, 0, 0, tzinfo=UTC)
    end = datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC)

    # 1. Hourly cron: 0 * * * * should produce 24 projected runs
    runs_hourly = pipeline_service.compute_projected_runs_for_day("0 * * * *", start, end)
    assert len(runs_hourly) == 24
    assert runs_hourly[0]["type"] == "projected"
    assert "scheduled_time" in runs_hourly[0]

    # 2. Daily cron: @daily should produce 1 projected run at midnight
    runs_daily = pipeline_service.compute_projected_runs_for_day("@daily", start, end)
    assert len(runs_daily) == 1
    assert "00:00:00" in runs_daily[0]["scheduled_time"]

    # 3. Human timetable description fallbacks
    runs_at_8 = pipeline_service.compute_projected_runs_for_day("At 08:00", start, end)
    assert len(runs_at_8) == 1
    assert "08:00:00" in runs_at_8[0]["scheduled_time"]

    runs_every_hour = pipeline_service.compute_projected_runs_for_day("Every hour", start, end)
    assert len(runs_every_hour) == 24

    runs_every_2h = pipeline_service.compute_projected_runs_for_day("Every 2 hours", start, end)
    assert len(runs_every_2h) == 12

    # 4. Non-standard or none interval produces empty list
    assert pipeline_service.compute_projected_runs_for_day(None, start, end) == []
    assert pipeline_service.compute_projected_runs_for_day("invalid-cron", start, end) == []


def test_pipeline_metadata_db_crud():
    from models import PipelineMetadata

    mock_db = MagicMock()
    ws_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_query = mock_db.query.return_value
    mock_query.filter.return_value.first.return_value = None

    created = pipeline_service.upsert_pipeline_metadata(
        workspace_id=ws_id,
        dag_id="dag_1",
        color_hex="#10B981",
        display_label="Customer Ingestion",
        is_visible_on_timeline=True,
        user_id=user_id,
        db=mock_db,
    )
    assert created["color_hex"] == "#10B981"
    assert created["display_label"] == "Customer Ingestion"
    assert mock_db.add.called
    assert mock_db.commit.called

    existing_meta = PipelineMetadata(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        dag_id="dag_1",
        color_hex="#10B981",
        display_label="Customer Ingestion",
        is_visible_on_timeline=True,
    )
    mock_query.filter.return_value.all.return_value = [existing_meta]
    meta_map = pipeline_service.get_pipeline_metadata_map(ws_id, mock_db)
    assert "dag_1" in meta_map
    assert meta_map["dag_1"].color_hex == "#10B981"


def test_pipeline_timeline_rest_endpoint():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "viewer"),
            ),
            patch.object(
                pipeline_service,
                "get_workspace_timeline",
                new_callable=AsyncMock,
                return_value={
                    "date": "2026-10-14",
                    "timezone": "UTC",
                    "window_start": "2026-10-14T00:00:00+00:00",
                    "window_end": "2026-10-14T23:59:59.999999+00:00",
                    "current_time": "2026-10-14T12:00:00+00:00",
                    "is_today": True,
                    "palette": pipeline_service.DEFAULT_PALETTE,
                    "pipelines": [
                        {
                            "dag_id": "ws_analytics_daily_etl",
                            "display_label": "Daily Analytics ETL",
                            "color_hex": "#3B82F6",
                            "is_visible_on_timeline": True,
                            "schedule_interval": "@daily",
                            "is_paused": False,
                            "runs": [
                                {
                                    "type": "actual",
                                    "dag_run_id": "manual__1",
                                    "state": "success",
                                    "scheduled_time": "2026-10-14T02:00:00Z",
                                    "duration_seconds": 95.0,
                                }
                            ],
                        }
                    ],
                },
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}
            res = client.get("/api/v1/pipelines/timeline?date=2026-10-14&tz=UTC", headers=headers)
            assert res.status_code == 200
            data = res.json()
            assert data["date"] == "2026-10-14"
            assert data["timezone"] == "UTC"
            assert len(data["pipelines"]) == 1
            assert data["pipelines"][0]["color_hex"] == "#3B82F6"
            assert len(data["pipelines"][0]["runs"]) == 1
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipeline_settings_get_and_patch_endpoints():
    user_editor, workspace, member, token_editor = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_editor

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "editor"),
            ),
            patch.object(
                pipeline_service,
                "get_pipeline_metadata_map",
                return_value={},
            ),
            patch.object(
                pipeline_service,
                "upsert_pipeline_metadata",
                return_value={
                    "workspace_id": str(workspace.id),
                    "dag_id": "ws_analytics_daily_etl",
                    "color_hex": "#10B981",
                    "display_label": "Renamed ETL",
                    "is_visible_on_timeline": True,
                    "updated_at": "2026-10-14T12:00:00Z",
                },
            ),
        ):
            headers = {"Authorization": f"Bearer {token_editor}"}

            # 1. GET settings (falls back to default color)
            r_get = client.get("/api/v1/pipelines/ws_analytics_daily_etl/settings", headers=headers)
            assert r_get.status_code == 200
            data_get = r_get.json()
            assert data_get["dag_id"] == "ws_analytics_daily_etl"
            assert "color_hex" in data_get
            assert "palette" in data_get

            # 2. PATCH settings as editor
            r_patch = client.patch(
                "/api/v1/pipelines/ws_analytics_daily_etl/settings",
                json={"color_hex": "#10B981", "display_label": "Renamed ETL"},
                headers=headers,
            )
            assert r_patch.status_code == 200
            data_patch = r_patch.json()
            assert data_patch["color_hex"] == "#10B981"
            assert data_patch["display_label"] == "Renamed ETL"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipeline_settings_patch_viewer_forbidden():
    user_viewer, workspace, member, token_viewer = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_viewer

    try:
        with patch(
            "pipelines_router.get_pipeline_workspace_and_role",
            side_effect=backend_main.HTTPException(
                403, "Workspace viewer role is read-only. Cannot modify or trigger pipelines."
            ),
        ):
            headers = {"Authorization": f"Bearer {token_viewer}"}
            r_patch = client.patch(
                "/api/v1/pipelines/ws_analytics_daily_etl/settings",
                json={"color_hex": "#EF4444"},
                headers=headers,
            )
            assert r_patch.status_code == 403
            assert "Workspace viewer role is read-only" in r_patch.text
    finally:
        backend_main.app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_cancel_dag_run_service():
    with (
        patch.object(pipeline_service, "get_dag_details", new_callable=AsyncMock),
        patch("httpx.AsyncClient.patch", new_callable=AsyncMock) as mock_patch,
    ):
        mock_patch.return_value = MagicMock(
            status_code=200,
            json=lambda: {"dag_run_id": "manual__1", "state": "failed"},
        )
        res = await pipeline_service.cancel_dag_run("analytics", "ws_analytics_etl", "manual__1")
        assert res["dag_id"] == "ws_analytics_etl"
        assert res["dag_run_id"] == "manual__1"
        assert res["state"] == "failed"
        assert "cancelled successfully" in res["message"]


def test_cancel_pipeline_run_endpoint_success():
    user_editor, workspace, member, token_editor = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_editor

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "editor"),
            ),
            patch.object(
                pipeline_service,
                "cancel_dag_run",
                new_callable=AsyncMock,
                return_value={
                    "dag_id": "ws_analytics_etl",
                    "dag_run_id": "manual__run_1",
                    "state": "failed",
                    "message": "DAG run cancelled successfully",
                },
            ),
        ):
            headers = {"Authorization": f"Bearer {token_editor}"}
            r = client.post(
                "/api/v1/pipelines/ws_analytics_etl/runs/manual__run_1/cancel",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["state"] == "failed"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipeline_settings_patch_description_and_name():
    user_editor, workspace, member, token_editor = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_editor

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "editor"),
            ),
            patch.object(
                pipeline_service,
                "upsert_pipeline_metadata",
                return_value={
                    "dag_id": "ws_analytics_etl",
                    "color_hex": "#8B5CF6",
                    "display_label": "Custom Pipeline Title",
                    "description": "Custom edited description",
                    "is_visible_on_timeline": True,
                },
            ) as mock_upsert,
        ):
            headers = {"Authorization": f"Bearer {token_editor}"}
            r = client.patch(
                "/api/v1/pipelines/ws_analytics_etl/settings",
                json={
                    "display_label": "Custom Pipeline Title",
                    "description": "Custom edited description",
                    "color_hex": "#8B5CF6",
                },
                headers=headers,
            )
            assert r.status_code == 200
            data = r.json()
            assert data["display_label"] == "Custom Pipeline Title"
            assert data["description"] == "Custom edited description"
            assert data["color_hex"] == "#8B5CF6"
            mock_upsert.assert_called_once()
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipeline_service_resolves_known_pipeline_notebooks():
    dag = {
        "dag_id": "ingest_tmdb_movies",
        "tags": [
            {"name": "ingestion"},
            {"name": "notebooks"},
            {"name": "workspace:default"},
        ],
        "is_paused": False,
    }
    with (
        patch("pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
        patch("pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
        patch("pipeline_service.httpx.AsyncClient") as mock_client,
    ):
        mock_instance = AsyncMock()
        mock_instance.get.return_value = MagicMock(status_code=200, json=lambda: {"dags": [dag]})
        mock_client.return_value.__aenter__.return_value = mock_instance
        res = asyncio.run(pipeline_service.list_workspace_dags("default", is_superadmin=True))
        assert len(res) == 1
        assert res[0]["dag_id"] == "ingest_tmdb_movies"
        assert res[0]["is_notebook_job"] is True
        assert res[0]["notebook_path"] == "tmdb_daily_movies_ingest.ipynb"


def test_pipeline_service_resolves_notebook_tag():
    dag = {
        "dag_id": "ws_default_custom",
        "tags": [{"name": "workspace:default"}, {"name": "notebook:custom.ipynb"}],
        "is_paused": False,
    }
    with (
        patch("pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
        patch("pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
        patch("pipeline_service.httpx.AsyncClient") as mock_client,
    ):
        mock_instance = AsyncMock()
        mock_instance.get.return_value = MagicMock(status_code=200, json=lambda: {"dags": [dag]})
        mock_client.return_value.__aenter__.return_value = mock_instance
        res = asyncio.run(pipeline_service.list_workspace_dags("default", is_superadmin=True))
        assert len(res) == 1
        assert res[0]["notebook_path"] == "custom.ipynb"


def test_extract_pending_dag_metadata_from_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        dag_file = Path(tmpdir) / "ws_default_clinical_trials_ingest.py"
        dag_file.write_text(
            """
with DAG(
    dag_id="ws_default_clinical_trials_ingest",
    schedule="@hourly",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:default", "notebook", "scheduled", "notebook:test-pipelines/clinical_trials_ingest.ipynb"],
    doc_md="Clinical Trials Ingest",
) as dag:
    pass
""",
            encoding="utf-8",
        )
        meta = pipeline_service._extract_pending_dag_metadata_from_file(dag_file, "default")
        assert meta is not None
        assert meta["dag_id"] == "ws_default_clinical_trials_ingest"
        assert meta["description"] == "Clinical Trials Ingest"
        assert meta["schedule_interval"] == {"__type": "CronExpression", "value": "@hourly"}
        assert meta["timetable_description"] == "@hourly"
        assert any(
            t["name"] == "notebook:test-pipelines/clinical_trials_ingest.ipynb"
            for t in meta["tags"]
        )


def test_list_workspace_dags_merges_pending_file_on_disk():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = Path(tmpdir)
        ws_dir = dags_dir / "workspaces" / "analytics"
        ws_dir.mkdir(parents=True)
        py_file = ws_dir / "ws_analytics_hourly_job.py"
        py_file.write_text(
            """
with DAG(
    dag_id="ws_analytics_hourly_job",
    schedule="@hourly",
    tags=["workspace:analytics", "notebook", "scheduled", "notebook:test.ipynb"],
    doc_md="Hourly Test Job",
) as dag:
    pass
""",
            encoding="utf-8",
        )
        with (
            patch("pipeline_service.get_airflow_dags_dir", return_value=dags_dir),
            patch("pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
            patch("pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
            patch("pipeline_service.httpx.AsyncClient") as mock_client,
        ):
            mock_instance = AsyncMock()
            # Airflow API returns empty list of dags (hasn't parsed the new file yet)
            mock_instance.get.return_value = MagicMock(status_code=200, json=lambda: {"dags": []})
            mock_client.return_value.__aenter__.return_value = mock_instance

            res = asyncio.run(
                pipeline_service.list_workspace_dags("analytics", is_superadmin=False)
            )
            assert len(res) == 1
            assert res[0]["dag_id"] == "ws_analytics_hourly_job"
            assert res[0]["schedule_interval"] == "@hourly"
            assert res[0]["is_notebook_job"] is True
            assert res[0]["notebook_path"] == "test.ipynb"


def test_get_dag_details_disk_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = Path(tmpdir)
        ws_dir = dags_dir / "workspaces" / "analytics"
        ws_dir.mkdir(parents=True)
        py_file = ws_dir / "ws_analytics_pending_job.py"
        py_file.write_text(
            """
with DAG(
    dag_id="ws_analytics_pending_job",
    schedule="@daily",
    tags=["workspace:analytics", "notebook", "scheduled", "notebook:pending.ipynb"],
    doc_md="Pending Job Title",
) as dag:
    pass
""",
            encoding="utf-8",
        )
        with (
            patch("pipeline_service.get_airflow_dags_dir", return_value=dags_dir),
            patch("pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
            patch("pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
            patch("pipeline_service.httpx.AsyncClient") as mock_client,
        ):
            mock_instance = AsyncMock()
            # Airflow API returns 404 (not yet parsed)
            mock_instance.get.return_value = MagicMock(status_code=404, text="Not Found")
            mock_client.return_value.__aenter__.return_value = mock_instance

            details = asyncio.run(
                pipeline_service.get_dag_details(
                    "analytics", "ws_analytics_pending_job", is_superadmin=False
                )
            )
            assert details["dag_id"] == "ws_analytics_pending_job"
            assert details["pipeline_name"] == "Pending Job Title"
            assert details["schedule_interval"] == "@daily"
            assert details["notebook_path"] == "pending.ipynb"
            assert details["last_run"] is None


# ── Schedule Update & Validation Tests (#188) ─────────────────────────────────


def test_validate_schedule_expression():
    from fastapi import HTTPException

    # Valid presets
    assert pipeline_service.validate_schedule_expression("@hourly") == "@hourly"
    assert pipeline_service.validate_schedule_expression("@daily") == "@daily"
    assert pipeline_service.validate_schedule_expression("@weekly") == "@weekly"
    assert pipeline_service.validate_schedule_expression("@monthly") == "@monthly"

    # None / Manual normalization
    assert pipeline_service.validate_schedule_expression(None) == "None"
    assert pipeline_service.validate_schedule_expression("none") == "None"
    assert pipeline_service.validate_schedule_expression("null") == "None"
    assert pipeline_service.validate_schedule_expression("") == "None"

    # Valid cron expressions
    assert pipeline_service.validate_schedule_expression("0 8 * * *") == "0 8 * * *"
    assert pipeline_service.validate_schedule_expression("*/15 * * * *") == "*/15 * * * *"

    # Invalid expressions raise 400
    with pytest.raises(HTTPException) as exc_info:
        pipeline_service.validate_schedule_expression("invalid_cron_expr")
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        pipeline_service.validate_schedule_expression("99 99 * * *")
    assert exc_info.value.status_code == 400


def test_replace_schedule_in_code():
    code_with_schedule = """
from airflow import DAG
with DAG(
    dag_id="test_dag",
    schedule="0 8 * * *",
    start_date=datetime(2026, 1, 1),
) as dag:
    pass
"""
    updated = pipeline_service._replace_schedule_in_code(code_with_schedule, "@hourly")
    assert 'schedule="@hourly"' in updated
    assert ast.parse(updated)

    none_updated = pipeline_service._replace_schedule_in_code(code_with_schedule, "None")
    assert "schedule=None" in none_updated
    assert ast.parse(none_updated)

    code_with_schedule_interval = """
from airflow import DAG
with DAG(
    dag_id="test_dag",
    schedule_interval="0 2 * * *",
    start_date=datetime(2026, 1, 1),
) as dag:
    pass
"""
    updated_interval = pipeline_service._replace_schedule_in_code(
        code_with_schedule_interval, "0 0 * * *"
    )
    assert 'schedule_interval="0 0 * * *"' in updated_interval
    assert ast.parse(updated_interval)


@pytest.mark.anyio
async def test_update_pipeline_schedule_workspace_dag():
    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = Path(tmpdir)
        ws_dir = dags_dir / "workspaces" / "analytics"
        ws_dir.mkdir(parents=True)
        py_file = ws_dir / "ws_analytics_etl.py"
        py_file.write_text(
            """
from airflow import DAG
with DAG(
    dag_id="ws_analytics_etl",
    schedule=None,
) as dag:
    pass
""",
            encoding="utf-8",
        )

        with patch("pipeline_service.get_airflow_dags_dir", return_value=dags_dir):
            res = await pipeline_service.update_pipeline_schedule(
                workspace_slug="analytics",
                dag_id="ws_analytics_etl",
                schedule_interval="@daily",
                is_superadmin=False,
                role="member",
            )
            assert res["dag_id"] == "ws_analytics_etl"
            assert res["schedule_interval"] == "@daily"
            assert res["status"] == "updated"

            # Verify file content on disk
            content = py_file.read_text(encoding="utf-8")
            assert 'schedule="@daily"' in content
            assert ast.parse(content)


@pytest.mark.anyio
async def test_update_pipeline_schedule_builtin_dag_rbac():
    from fastapi import HTTPException

    with tempfile.TemporaryDirectory() as tmpdir:
        dags_dir = Path(tmpdir)
        py_file = dags_dir / "ingest_tmdb_movies_dag.py"
        py_file.write_text(
            """
from airflow import DAG
with DAG(
    dag_id="ingest_tmdb_movies",
    schedule="0 2 * * *",
) as dag:
    pass
""",
            encoding="utf-8",
        )

        with patch("pipeline_service.get_airflow_dags_dir", return_value=dags_dir):
            # Member role should be rejected with 403
            with pytest.raises(HTTPException) as exc_info:
                await pipeline_service.update_pipeline_schedule(
                    workspace_slug="analytics",
                    dag_id="ingest_tmdb_movies",
                    schedule_interval="0 4 * * *",
                    is_superadmin=False,
                    role="member",
                )
            assert exc_info.value.status_code == 403
            assert "Only workspace admins or superadmins" in exc_info.value.detail

            # Admin role should succeed
            res = await pipeline_service.update_pipeline_schedule(
                workspace_slug="analytics",
                dag_id="ingest_tmdb_movies",
                schedule_interval="0 4 * * *",
                is_superadmin=False,
                role="admin",
            )
            assert res["dag_id"] == "ingest_tmdb_movies"
            assert res["schedule_interval"] == "0 4 * * *"

            content = py_file.read_text(encoding="utf-8")
            assert 'schedule="0 4 * * *"' in content


@pytest.mark.anyio
async def test_update_pipeline_schedule_run_notebook_blocked():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.update_pipeline_schedule(
            workspace_slug="analytics",
            dag_id="run_notebook",
            schedule_interval="@hourly",
            is_superadmin=True,
            role="superadmin",
        )
    assert exc_info.value.status_code == 400
    assert "run_notebook cannot be modified" in exc_info.value.detail


def test_pipeline_schedule_patch_endpoint():
    user_admin, workspace, member, token_admin = _generate_test_user_and_token(role="admin")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_admin

    try:
        with (
            patch(
                "pipelines_router.get_pipeline_workspace_and_role",
                return_value=(workspace, "admin"),
            ),
            patch.object(
                pipeline_service,
                "update_pipeline_schedule",
                new_callable=AsyncMock,
                return_value={
                    "dag_id": "ws_analytics_etl",
                    "schedule_interval": "@weekly",
                    "file_path": "/dags/ws_analytics_etl.py",
                    "status": "updated",
                },
            ) as mock_update,
        ):
            headers = {"Authorization": f"Bearer {token_admin}"}
            resp = client.patch(
                "/api/v1/pipelines/ws_analytics_etl/schedule",
                json={"schedule_interval": "@weekly"},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["dag_id"] == "ws_analytics_etl"
            assert data["schedule_interval"] == "@weekly"
            assert mock_update.called
    finally:
        backend_main.app.dependency_overrides.clear()


def test_pipeline_schedule_patch_viewer_forbidden():
    user_viewer, workspace, member, token_viewer = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_viewer

    try:
        from fastapi import HTTPException

        with patch(
            "pipelines_router.get_pipeline_workspace_and_role",
            side_effect=HTTPException(403, "Workspace viewer role is read-only"),
        ):
            headers = {"Authorization": f"Bearer {token_viewer}"}
            resp = client.patch(
                "/api/v1/pipelines/ws_analytics_etl/schedule",
                json={"schedule_interval": "@weekly"},
                headers=headers,
            )
            assert resp.status_code == 403
    finally:
        backend_main.app.dependency_overrides.clear()
