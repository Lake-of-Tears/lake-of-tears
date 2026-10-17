from __future__ import annotations

import re
from pathlib import Path

COMPOSE = (Path(__file__).parents[1] / "docker-compose.yml").read_text()


def _airflow_entrypoint() -> str:
    block = re.search(r"\n  airflow:\n(.*?)\n    environment:", COMPOSE, re.S)
    assert block, "airflow service not found in docker-compose.yml"
    return block.group(1)


def test_airflow_admin_password_is_applied_before_standalone_starts():
    script = _airflow_entrypoint()
    reset = script.find("airflow users reset-password")
    standalone = script.find("exec /entrypoint standalone")
    assert reset != -1, "the configured admin password is never applied"
    assert reset < standalone
    # `airflow standalone` would otherwise generate its own random admin password
    assert "_AIRFLOW_WWW_USER_PASSWORD" in script[reset:standalone]


def test_airflow_creates_the_user_before_resetting_its_password():
    script = _airflow_entrypoint()
    assert script.find("airflow users create") < script.find("airflow users reset-password")
    # `users create` exits 0 when the user already exists, so it must not be the only step
    assert (
        ";"
        in script[script.find("airflow users create") : script.find("airflow users reset-password")]
    )


def test_airflow_removes_a_stale_webserver_pid_file_on_start():
    # A leftover pid file from the previous container run stops the web server after a restart.
    assert "rm -f /opt/airflow/airflow-webserver.pid" in _airflow_entrypoint()
