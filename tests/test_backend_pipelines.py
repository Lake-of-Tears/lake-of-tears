from __future__ import annotations

import ast
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

    # Superadmin bypass
    assert pipeline_service.is_dag_accessible_by_workspace(
        dag_tagged, "finance", is_superadmin=True
    )

    # Global run_notebook is accessible
    assert pipeline_service.is_dag_accessible_by_workspace(
        {"dag_id": "run_notebook", "tags": []}, "finance"
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

    # 3. Non-standard or none interval produces empty list
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

