"""Unit and Helm template tests for Issue #168: Migrate Airflow to KubernetesExecutor."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "lake-of-tears"
VALUES_LOCAL = REPO_ROOT / "deploy" / "helm" / "values-local.example.yaml"

backend_dir = str(REPO_ROOT / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from bundles.deployer import generate_bundle_dag_code  # noqa: E402
from bundles.schemas import PipelineResource, PipelineTask  # noqa: E402
from pipeline_service import generate_dynamic_dag_code  # noqa: E402
from services_manager.k8s_manager import SERVICE_METADATA, KubernetesServiceManager  # noqa: E402


def _render_helm(*extra_args: str) -> list[dict]:
    cmd = [
        "helm",
        "template",
        "lake-of-tears",
        str(CHART_DIR),
        "--namespace",
        "lake-of-tears",
        "-f",
        str(VALUES_LOCAL),
        *extra_args,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(res.stdout) if doc]


def _env_dict(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def test_airflow_dockerfile_includes_kubernetes_and_amazon_providers():
    dockerfile = (REPO_ROOT / "deploy" / "airflow" / "Dockerfile").read_text(encoding="utf-8")
    assert "apache-airflow-providers-cncf-kubernetes" in dockerfile
    assert "apache-airflow-providers-amazon" in dockerfile


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not found")
def test_airflow_topology_decoupling_and_kubernetes_executor_defaults():
    docs = _render_helm()
    deployments = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}

    # Monolithic deployment must be replaced by separate webserver and scheduler deployments
    assert "lake-of-tears-airflow" not in deployments
    assert "lake-of-tears-airflow-webserver" in deployments
    assert "lake-of-tears-airflow-scheduler" in deployments

    webserver = deployments["lake-of-tears-airflow-webserver"]
    scheduler = deployments["lake-of-tears-airflow-scheduler"]

    # Webserver runs without elevated cluster permissions
    ws_pod_spec = webserver["spec"]["template"]["spec"]
    assert ws_pod_spec.get("automountServiceAccountToken") is False
    ws_container = ws_pod_spec["containers"][0]
    assert "exec airflow webserver" in ws_container["args"][0]

    # Webserver waits for DB migrations via initContainer
    ws_inits = {c["name"]: c for c in ws_pod_spec.get("initContainers", [])}
    assert "wait-for-airflow-migrations" in ws_inits
    assert "airflow db check-migrations" in ws_inits["wait-for-airflow-migrations"]["args"][0]

    # Scheduler uses dedicated ServiceAccount and runs db migrate in initContainer
    sched_pod_spec = scheduler["spec"]["template"]["spec"]
    assert sched_pod_spec.get("serviceAccountName") == "lake-of-tears-airflow-scheduler"
    sched_inits = {c["name"]: c for c in sched_pod_spec.get("initContainers", [])}
    assert "airflow-db-init" in sched_inits
    assert "airflow db migrate" in sched_inits["airflow-db-init"]["args"][0]
    assert "airflow users create" in sched_inits["airflow-db-init"]["args"][0]

    sched_container = sched_pod_spec["containers"][0]
    assert "exec airflow scheduler" in sched_container["args"][0]

    # Verify KubernetesExecutor environment variables and pod lifecycle policy
    for container in (ws_container, sched_container):
        env = _env_dict(container)
        assert env["AIRFLOW__CORE__EXECUTOR"]["value"] == "KubernetesExecutor"
        assert env["AIRFLOW__KUBERNETES_EXECUTOR__NAMESPACE"]["value"] == "lake-of-tears"
        assert (
            env["AIRFLOW__KUBERNETES_EXECUTOR__POD_TEMPLATE_FILE"]["value"]
            == "/opt/airflow/pod_templates/pod_template_file.yaml"
        )
        assert env["AIRFLOW__KUBERNETES_EXECUTOR__DELETE_WORKER_PODS"]["value"] == "True"
        assert (
            env["AIRFLOW__KUBERNETES_EXECUTOR__DELETE_WORKER_PODS_ON_FAILURE"]["value"] == "False"
        )

    # Service still named lake-of-tears-airflow and routes to webserver
    services = {s["metadata"]["name"]: s for s in docs if s.get("kind") == "Service"}
    assert "lake-of-tears-airflow" in services
    assert (
        services["lake-of-tears-airflow"]["spec"]["selector"]["app.kubernetes.io/component"]
        == "airflow-webserver"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not found")
def test_airflow_scheduler_rbac_least_privilege():
    docs = _render_helm()
    service_accounts = {
        sa["metadata"]["name"]: sa for sa in docs if sa.get("kind") == "ServiceAccount"
    }
    roles = {r["metadata"]["name"]: r for r in docs if r.get("kind") == "Role"}
    bindings = {rb["metadata"]["name"]: rb for rb in docs if rb.get("kind") == "RoleBinding"}

    assert "lake-of-tears-airflow-scheduler" in service_accounts
    assert "lake-of-tears-airflow-scheduler" in roles
    assert "lake-of-tears-airflow-scheduler-binding" in bindings

    role = roles["lake-of-tears-airflow-scheduler"]
    assert role["metadata"]["namespace"] == "lake-of-tears"

    rules = role["rules"]
    pod_rule = next(r for r in rules if "pods" in r["resources"])
    assert set(pod_rule["resources"]) == {"pods", "pods/log", "pods/exec"}
    assert set(pod_rule["verbs"]) == {
        "get",
        "list",
        "watch",
        "create",
        "update",
        "patch",
        "delete",
    }

    cm_secret_rule = next(r for r in rules if "configmaps" in r["resources"])
    assert set(cm_secret_rule["resources"]) == {"configmaps", "secrets"}
    assert "get" in cm_secret_rule["verbs"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm binary not found")
def test_airflow_pod_template_configmap_and_remote_logging_toggle():
    docs = _render_helm("--set", "airflow.remoteLogging.enabled=true")
    configmaps = {cm["metadata"]["name"]: cm for cm in docs if cm.get("kind") == "ConfigMap"}

    assert "lake-of-tears-airflow-pod-template" in configmaps
    cm = configmaps["lake-of-tears-airflow-pod-template"]
    pod_template_raw = cm["data"]["pod_template_file.yaml"]
    pod_template = yaml.safe_load(pod_template_raw)

    assert pod_template["kind"] == "Pod"
    assert pod_template["spec"]["restartPolicy"] == "Never"
    assert pod_template["spec"]["automountServiceAccountToken"] is False

    base_container = pod_template["spec"]["containers"][0]
    assert base_container["name"] == "base"
    assert base_container["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert base_container["resources"]["limits"] == {"cpu": "1000m", "memory": "2Gi"}

    mounts = {m["name"]: m["mountPath"] for m in base_container["volumeMounts"]}
    assert mounts["airflow-home"] == "/opt/airflow"

    env = _env_dict(base_container)
    assert env["AIRFLOW__LOGGING__REMOTE_LOGGING"]["value"] == "True"
    assert (
        env["AIRFLOW__LOGGING__REMOTE_BASE_LOG_FOLDER"]["value"]
        == "s3://datalake/_system/airflow-logs"
    )
    assert env["AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID"]["value"] == "minio_s3"


def test_dynamic_dag_generation_includes_k8s_executor_config():
    dag_code = generate_dynamic_dag_code(
        workspace_slug="acme",
        dag_id="ws_acme__ingest_tmdb_movies",
        notebook_path="ingest_tmdb_movies.ipynb",
        schedule_interval="@daily",
        pipeline_title="Ingest TMDB Movies",
        compute_resources={"cpu": "1", "memory": "2Gi"},
        mode="docker",
    )
    assert "def _build_k8s_executor_config():" in dag_code
    assert 'requests={"cpu": "1", "memory": "2Gi"}' in dag_code
    assert 'limits={"cpu": "2", "memory": "4Gi"}' in dag_code
    assert "executor_config=_build_k8s_executor_config()" in dag_code


def test_bundle_dag_generation_supports_per_task_k8s_resources():
    pipeline = PipelineResource(
        name="tmdb_etl",
        schedule="@daily",
        tasks=[
            PipelineTask(
                name="extract_movies",
                type="python_script",
                entrypoint="src/extract.py",
                resources={"cpu": "500m", "memory": "1Gi", "cpu_limit": "2", "memory_limit": "4Gi"},
            )
        ],
    )
    dag_id, code = generate_bundle_dag_code(
        workspace_slug="acme",
        bundle_name="movies_bundle",
        target_name="dev",
        pipeline=pipeline,
    )
    assert dag_id == "ws_acme_movies_bundle_tmdb_etl"
    assert 'executor_config=_build_k8s_executor_config("500m", "1Gi", "2", "4Gi")' in code


def test_k8s_service_manager_supports_split_airflow_deployments():
    assert "airflow-webserver" in SERVICE_METADATA
    assert "airflow-scheduler" in SERVICE_METADATA

    mgr = KubernetesServiceManager(namespace="lake-of-tears")
    mock_deps = [
        {"metadata": {"name": "lake-of-tears-airflow-webserver"}, "spec": {"replicas": 1}},
        {"metadata": {"name": "lake-of-tears-airflow-scheduler"}, "spec": {"replicas": 1}},
    ]
    with patch.object(mgr, "_get_deployments", return_value=mock_deps):
        assert mgr._resolve_deployment_name("airflow-webserver") == "lake-of-tears-airflow-webserver"
        assert mgr._resolve_deployment_name("airflow-scheduler") == "lake-of-tears-airflow-scheduler"
        # Legacy 'airflow' alias resolves cleanly to webserver when split
        assert mgr._resolve_deployment_name("airflow") == "lake-of-tears-airflow-webserver"
