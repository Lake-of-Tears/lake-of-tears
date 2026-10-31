from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
AIRFLOW_TEMPLATE = ROOT / "deploy" / "helm" / "lake-of-tears" / "templates" / "airflow.yaml"
INGRESS_TEMPLATE = ROOT / "deploy" / "helm" / "lake-of-tears" / "templates" / "ingress.yaml"


def test_helm_airflow_base_url_has_no_trailing_slash():
    content = AIRFLOW_TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"name:\s*AIRFLOW__WEBSERVER__BASE_URL\s*\n\s*value:\s*\"([^\"]+)\"", content)
    assert (
        match
    ), "AIRFLOW__WEBSERVER__BASE_URL is not set in deploy/helm/lake-of-tears/templates/airflow.yaml"
    # Airflow 2.10 refuses to start: "webserver.base_url conf cannot have a trailing slash"
    val = match.group(1)
    assert not val.endswith("/"), f"BASE_URL '{val}' ends with a slash"
    assert val.endswith("/airflow"), f"BASE_URL '{val}' should end with /airflow"


def test_helm_ingress_keeps_the_airflow_prefix():
    content = INGRESS_TEMPLATE.read_text(encoding="utf-8")
    assert "path: /airflow" in content, "path /airflow not found in ingress.yaml"
    assert (
        "name: {{ $fullName }}-airflow" in content
    ), "backend service for /airflow not found in ingress.yaml"
