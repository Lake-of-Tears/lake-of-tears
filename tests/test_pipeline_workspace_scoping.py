from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# Stub airflow and papermill modules for headless testing
airflow = types.ModuleType("airflow")
airflow.DAG = MagicMock()
operators = types.ModuleType("airflow.operators")
python = types.ModuleType("airflow.operators.python")
python.PythonOperator = MagicMock()
sys.modules["airflow"] = airflow
sys.modules["airflow.operators"] = operators
sys.modules["airflow.operators.python"] = python

papermill = types.ModuleType("papermill")
papermill.execute_notebook = MagicMock()
sys.modules["papermill"] = papermill

from backend import pipeline_service  # noqa: E402
from pipeline.dags import run_notebook_dag  # noqa: E402


def test_dag_accessibility_strict_workspace_isolation():
    """Verify is_dag_accessible_by_workspace strictly enforces prefix and workspace tags."""
    # Prefix-based
    dag_alpha = {"dag_id": "ws_alpha_etl", "tags": []}
    assert pipeline_service.is_dag_accessible_by_workspace(dag_alpha, "alpha")
    assert not pipeline_service.is_dag_accessible_by_workspace(dag_alpha, "beta")
    assert not pipeline_service.is_dag_accessible_by_workspace(dag_alpha, "default")

    # Tag-based
    dag_beta = {"dag_id": "custom_sync", "tags": [{"name": "workspace:beta"}]}
    assert pipeline_service.is_dag_accessible_by_workspace(dag_beta, "beta")
    assert not pipeline_service.is_dag_accessible_by_workspace(dag_beta, "alpha")
    assert not pipeline_service.is_dag_accessible_by_workspace(dag_beta, "default")

    # Global runner run_notebook is excluded from all workspaces
    run_nb = {"dag_id": "run_notebook", "tags": [{"name": "notebooks"}]}
    assert not pipeline_service.is_dag_accessible_by_workspace(run_nb, "alpha")
    assert not pipeline_service.is_dag_accessible_by_workspace(run_nb, "default")
    assert not pipeline_service.is_dag_accessible_by_workspace(run_nb, "alpha", is_superadmin=True)

    # Untagged / system DAGs are excluded from all workspaces including default
    untagged_system = {"dag_id": "refresh_catalog_schemas_dag", "tags": [{"name": "system"}]}
    assert not pipeline_service.is_dag_accessible_by_workspace(untagged_system, "default")
    assert not pipeline_service.is_dag_accessible_by_workspace(untagged_system, "alpha")


def test_list_workspace_dags_filters_strictly():
    """Verify list_workspace_dags only returns active workspace DAGs and excludes run_notebook."""
    airflow_dags = [
        {"dag_id": "run_notebook", "tags": [{"name": "notebooks"}], "is_paused": False},
        {"dag_id": "refresh_catalog_schemas_dag", "tags": [{"name": "system"}], "is_paused": False},
        {"dag_id": "ws_alpha_metrics", "tags": [{"name": "workspace:alpha"}], "is_paused": False},
        {"dag_id": "ws_beta_payroll", "tags": [{"name": "workspace:beta"}], "is_paused": False},
    ]

    with (
        patch("backend.pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
        patch("backend.pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
        patch("backend.pipeline_service.httpx.AsyncClient") as mock_client,
    ):
        mock_instance = AsyncMock()
        mock_instance.get.return_value = MagicMock(
            status_code=200, json=lambda: {"dags": airflow_dags}
        )
        mock_client.return_value.__aenter__.return_value = mock_instance

        # Query for workspace alpha
        alpha_res = asyncio.run(pipeline_service.list_workspace_dags("alpha"))
        alpha_ids = [d["dag_id"] for d in alpha_res]
        assert alpha_ids == ["ws_alpha_metrics"]

        # Query for workspace beta
        beta_res = asyncio.run(pipeline_service.list_workspace_dags("beta"))
        beta_ids = [d["dag_id"] for d in beta_res]
        assert beta_ids == ["ws_beta_payroll"]

        # Query for default workspace (neither alpha, beta, run_notebook, nor untagged refresh_catalog appear)
        default_res = asyncio.run(pipeline_service.list_workspace_dags("default"))
        default_ids = [d["dag_id"] for d in default_res]
        assert default_ids == []


def test_get_dag_details_run_notebook_not_found():
    """Verify get_dag_details returns 404 for run_notebook."""
    with (
        patch("backend.pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
        patch("backend.pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
        patch("backend.pipeline_service.httpx.AsyncClient") as mock_client,
    ):
        mock_instance = AsyncMock()
        mock_instance.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"dag_id": "run_notebook", "tags": [{"name": "notebooks"}]},
        )
        mock_client.return_value.__aenter__.return_value = mock_instance

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(pipeline_service.get_dag_details("alpha", "run_notebook"))
        assert exc_info.value.status_code == 404


def test_trigger_dag_run_blocks_direct_run_notebook():
    """Verify direct triggering of run_notebook via pipeline trigger is blocked."""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(pipeline_service.trigger_dag_run("alpha", "run_notebook"))
    assert exc_info.value.status_code == 400
    assert "Direct triggering of 'run_notebook'" in exc_info.value.detail


def test_run_notebook_dag_runs_isolation():
    """Verify list_dag_runs filters run_notebook runs strictly by workspace_slug."""
    runs_data = [
        {
            "dag_run_id": "run_alpha_1",
            "state": "success",
            "start_date": "2026-10-25T10:00:00Z",
            "end_date": "2026-10-25T10:01:00Z",
            "conf": {"workspace_slug": "alpha", "notebook_path": "analysis.ipynb"},
        },
        {
            "dag_run_id": "run_beta_1",
            "state": "success",
            "start_date": "2026-10-25T11:00:00Z",
            "end_date": "2026-10-25T11:02:00Z",
            "conf": {"workspace_slug": "beta", "notebook_path": "model.ipynb"},
        },
    ]

    with (
        patch("backend.pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
        patch("backend.pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
        patch("backend.pipeline_service.httpx.AsyncClient") as mock_client,
    ):
        mock_instance = AsyncMock()
        mock_instance.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"dag_runs": runs_data},
        )
        mock_client.return_value.__aenter__.return_value = mock_instance

        # Workspace alpha only sees run_alpha_1
        alpha_runs = asyncio.run(pipeline_service.list_dag_runs("alpha", "run_notebook"))
        assert len(alpha_runs) == 1
        assert alpha_runs[0]["dag_run_id"] == "run_alpha_1"
        assert alpha_runs[0]["conf"]["workspace_slug"] == "alpha"

        # Workspace beta only sees run_beta_1
        beta_runs = asyncio.run(pipeline_service.list_dag_runs("beta", "run_notebook"))
        assert len(beta_runs) == 1
        assert beta_runs[0]["dag_run_id"] == "run_beta_1"
        assert beta_runs[0]["conf"]["workspace_slug"] == "beta"


def test_run_notebook_get_run_and_logs_cross_workspace_protection():
    """Verify get_dag_run and get_task_log reject foreign workspace runs with 404."""
    alpha_run = {
        "dag_run_id": "run_alpha_1",
        "state": "success",
        "start_date": "2026-10-25T10:00:00Z",
        "end_date": "2026-10-25T10:01:00Z",
        "conf": {"workspace_slug": "alpha", "notebook_path": "secret_alpha.ipynb"},
    }

    with (
        patch("backend.pipeline_service.get_airflow_server_url", return_value="http://mock:8080"),
        patch("backend.pipeline_service.get_airflow_auth", return_value=("admin", "pass")),
        patch("backend.pipeline_service.httpx.AsyncClient") as mock_client,
    ):
        mock_instance = AsyncMock()
        mock_instance.get.return_value = MagicMock(status_code=200, json=lambda: alpha_run)
        mock_client.return_value.__aenter__.return_value = mock_instance

        # Alpha can access its own run
        run_res = asyncio.run(pipeline_service.get_dag_run("alpha", "run_notebook", "run_alpha_1"))
        assert run_res["dag_run_id"] == "run_alpha_1"

        # Beta querying alpha's run gets 404 Not Found
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(pipeline_service.get_dag_run("beta", "run_notebook", "run_alpha_1"))
        assert exc_info.value.status_code == 404

        # Beta querying task logs for alpha's run gets 404 Not Found
        with pytest.raises(HTTPException) as log_exc_info:
            asyncio.run(
                pipeline_service.get_task_log(
                    "beta",
                    "run_notebook",
                    "run_alpha_1",
                    "execute_and_archive_notebook",
                )
            )
        assert log_exc_info.value.status_code == 404


def test_update_pipeline_schedule_cross_workspace_denied():
    """Verify update_pipeline_schedule blocks modifying DAGs belonging to other workspaces."""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            pipeline_service.update_pipeline_schedule(
                workspace_slug="alpha",
                dag_id="ws_beta_payroll",
                schedule_interval="@daily",
            )
        )
    assert exc_info.value.status_code == 404
    assert "not found in workspace 'alpha'" in exc_info.value.detail


def test_run_notebook_dag_injects_workspace_parameters_and_env():
    """Verify run_notebook_dag executes Papermill with injected WORKSPACE_SLUG parameters and env."""
    mock_dag_run = MagicMock()
    mock_dag_run.conf = {
        "workspace_slug": "finance",
        "notebook_path": "quarterly_report.ipynb",
        "parameters": {"report_year": 2026},
        "triggered_by": "cfo@company.com",
    }

    executed_params = {}

    def mock_pm_execute(input_path, output_path, parameters, log_output):
        nonlocal executed_params
        executed_params = dict(parameters)

    with (
        patch("pipeline.dags.run_notebook_dag._fetch_source_notebook", return_value={"cells": []}),
        patch(
            "pipeline.dags.run_notebook_dag._upload_artifact_to_s3",
            return_value="notebook_runs/finance/quarterly_report/20261025.ipynb",
        ) as mock_upload,
        patch("papermill.execute_notebook", side_effect=mock_pm_execute),
    ):
        res = run_notebook_dag.execute_notebook_task(dag_run=mock_dag_run)
        assert res["status"] == "success"
        assert res["workspace_slug"] == "finance"
        assert res["notebook_path"] == "quarterly_report.ipynb"

        # Verify Papermill parameter injection
        assert executed_params["WORKSPACE_SLUG"] == "finance"
        assert executed_params["WORKSPACE"] == "finance"
        assert executed_params["NOTEBOOK_PATH"] == "quarterly_report.ipynb"
        assert executed_params["report_year"] == 2026
        assert "MINIO_ENDPOINT" in executed_params
        assert "MINIO_BUCKET" in executed_params

        # Verify environment variable injection
        assert os.environ.get("WORKSPACE_SLUG") == "finance"
        assert os.environ.get("WORKSPACE") == "finance"

        # Verify S3 upload destination was called with workspace_slug
        mock_upload.assert_called_once()
        args = mock_upload.call_args[0]
        assert args[0] == "finance"
        assert args[1] == "quarterly_report.ipynb"


def test_dynamic_dag_generation_injects_workspace_params():
    """Verify generated dynamic scheduled DAG code includes workspace parameter injection."""
    code = pipeline_service.generate_dynamic_dag_code(
        dag_id="ws_analytics_daily_churn",
        workspace_slug="analytics",
        notebook_path="churn.ipynb",
        schedule_interval="0 0 * * *",
        pipeline_title="Daily Churn",
    )
    assert "ws_analytics_daily_churn" in code
    assert '"workspace:analytics"' in code
    assert '"WORKSPACE_SLUG": workspace_slug' in code
    assert 'os.environ["WORKSPACE_SLUG"] = workspace_slug' in code
