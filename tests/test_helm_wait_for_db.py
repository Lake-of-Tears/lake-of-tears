import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).parents[1] / "deploy/helm/lake-of-tears"
VALUES_LOCAL = Path(__file__).parents[1] / "deploy/helm/values-local.example.yaml"


def test_helm_template_files_exist():
    assert (CHART_DIR / "templates/backend.yaml").exists()
    assert (CHART_DIR / "templates/airflow.yaml").exists()
    assert (CHART_DIR / "values.yaml").exists()


def test_values_yaml_contains_wait_for_db():
    values_content = (CHART_DIR / "values.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(values_content)

    assert "waitForDb" in parsed.get("backend", {})
    assert parsed["backend"]["waitForDb"]["enabled"] is True
    assert parsed["backend"]["waitForDb"]["timeoutSeconds"] == 60

    assert "waitForDb" in parsed.get("airflow", {})
    assert parsed["airflow"]["waitForDb"]["enabled"] is True
    assert parsed["airflow"]["waitForDb"]["timeoutSeconds"] == 60


def test_backend_and_airflow_templates_contain_wait_for_postgres_initcontainer():
    backend_content = (CHART_DIR / "templates/backend.yaml").read_text(encoding="utf-8")
    assert "name: wait-for-postgres" in backend_content
    assert "pg_isready" in backend_content
    assert "waitForDb" in backend_content

    airflow_content = (CHART_DIR / "templates/airflow.yaml").read_text(encoding="utf-8")
    assert "name: wait-for-postgres" in airflow_content
    assert "pg_isready" in airflow_content
    assert "waitForDb" in airflow_content


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not found")
def test_helm_template_rendering_default_wait_for_db():
    cmd = [
        "helm",
        "template",
        "lake-of-tears",
        str(CHART_DIR),
        "--namespace",
        "lake-of-tears",
        "-f",
        str(VALUES_LOCAL),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    manifests = list(yaml.safe_load_all(result.stdout))

    deployments = {
        m["metadata"]["name"]: m for m in manifests if m and m.get("kind") == "Deployment"
    }

    assert "lake-of-tears-backend" in deployments
    backend_inits = deployments["lake-of-tears-backend"]["spec"]["template"]["spec"].get(
        "initContainers", []
    )
    assert any(c["name"] == "wait-for-postgres" for c in backend_inits)
    backend_wait = next(c for c in backend_inits if c["name"] == "wait-for-postgres")
    assert "pg_isready" in backend_wait["command"][2]
    assert "timeout=60" in backend_wait["command"][2]

    assert "lake-of-tears-airflow" in deployments
    airflow_inits = deployments["lake-of-tears-airflow"]["spec"]["template"]["spec"].get(
        "initContainers", []
    )
    assert any(c["name"] == "wait-for-postgres" for c in airflow_inits)
    airflow_wait = next(c for c in airflow_inits if c["name"] == "wait-for-postgres")
    assert "pg_isready" in airflow_wait["command"][2]
    assert "timeout=60" in airflow_wait["command"][2]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not found")
def test_helm_template_rendering_disabled_wait_for_db():
    cmd = [
        "helm",
        "template",
        "lake-of-tears",
        str(CHART_DIR),
        "--namespace",
        "lake-of-tears",
        "-f",
        str(VALUES_LOCAL),
        "--set",
        "backend.waitForDb.enabled=false",
        "--set",
        "airflow.waitForDb.enabled=false",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    manifests = list(yaml.safe_load_all(result.stdout))

    deployments = {
        m["metadata"]["name"]: m for m in manifests if m and m.get("kind") == "Deployment"
    }

    backend_inits = deployments["lake-of-tears-backend"]["spec"]["template"]["spec"].get(
        "initContainers", []
    )
    assert not any(c["name"] == "wait-for-postgres" for c in backend_inits)

    airflow_inits = deployments["lake-of-tears-airflow"]["spec"]["template"]["spec"].get(
        "initContainers", []
    )
    assert not any(c["name"] == "wait-for-postgres" for c in airflow_inits)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not found")
def test_helm_template_rendering_disabled_postgres():
    cmd = [
        "helm",
        "template",
        "lake-of-tears",
        str(CHART_DIR),
        "--namespace",
        "lake-of-tears",
        "-f",
        str(VALUES_LOCAL),
        "--set",
        "postgres.enabled=false",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    manifests = list(yaml.safe_load_all(result.stdout))

    deployments = {
        m["metadata"]["name"]: m for m in manifests if m and m.get("kind") == "Deployment"
    }

    assert "lake-of-tears-postgres" not in deployments

    backend_inits = deployments["lake-of-tears-backend"]["spec"]["template"]["spec"].get(
        "initContainers", []
    )
    assert not any(c["name"] == "wait-for-postgres" for c in backend_inits)

    airflow_inits = deployments["lake-of-tears-airflow"]["spec"]["template"]["spec"].get(
        "initContainers", []
    )
    assert not any(c["name"] == "wait-for-postgres" for c in airflow_inits)
