from __future__ import annotations

import re
from pathlib import Path

AIRFLOW_TEMPLATE = (
    Path(__file__).parents[1] / "deploy" / "helm" / "lake-of-tears" / "templates" / "airflow.yaml"
).read_text(encoding="utf-8")


def _airflow_entrypoint() -> str:
    match = re.search(r"args:\s*\n\s*-\s*\|\s*\n(.*?)(?=\n\s*env:)", AIRFLOW_TEMPLATE, re.S)
    assert (
        match
    ), "airflow container args not found in deploy/helm/lake-of-tears/templates/airflow.yaml"
    return match.group(1)


def test_airflow_admin_password_is_applied():
    script = _airflow_entrypoint()
    reset = script.find("airflow users reset-password")
    assert reset != -1, "the configured admin password is never applied"
    assert "_AIRFLOW_WWW_USER_PASSWORD" in script[reset:]


def test_airflow_executes_standalone_entrypoint():
    script = _airflow_entrypoint()
    assert "exec /entrypoint standalone" in script


def test_airflow_removes_a_stale_webserver_pid_file_on_start():
    # A leftover pid file from the previous container run stops the web server after a restart.
    assert "rm -f /opt/airflow/airflow-webserver.pid" in _airflow_entrypoint()
