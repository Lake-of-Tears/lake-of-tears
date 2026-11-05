from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger("lake.backend.services_manager")

SAFEGUARDED_SERVICES = {
    "backend",
    "lakeui",
    "lake-of-tears-backend",
    "lake-of-tears-lakeui",
}

SERVICE_METADATA: dict[str, dict[str, Any]] = {
    "airflow": {
        "display_name": "Apache Airflow",
        "description": "Workflow orchestration, DAG execution & scheduled data pipelines.",
        "category": "Orchestration",
        "port": 8080,
    },
    "backend": {
        "display_name": "Lake Backend API",
        "description": "Core authentication, metadata catalog, SQL engine & REST endpoints.",
        "category": "Core Platform",
        "port": 8000,
    },
    "jupyter": {
        "display_name": "JupyterLab",
        "description": "Interactive notebook kernels, PySpark and Python development runtime.",
        "category": "Analytics",
        "port": 8888,
    },
    "lakeui": {
        "display_name": "Lake UI",
        "description": "NextGen web application frontend, management consoles & data views.",
        "category": "Core Platform",
        "port": 80,
    },
    "minio": {
        "display_name": "MinIO Object Storage",
        "description": "S3-compatible data lake storage, Parquet files & Delta Lake tables.",
        "category": "Storage",
        "port": 9000,
    },
    "postgres": {
        "display_name": "PostgreSQL Database",
        "description": "Relational application store, identity metadata & pipeline state.",
        "category": "Database",
        "port": 5432,
    },
    "superset": {
        "display_name": "Apache Superset",
        "description": "Business intelligence, chart builder, dashboards & visual analytics.",
        "category": "Analytics",
        "port": 8088,
    },
}


@dataclass
class ServiceInfo:
    id: str
    name: str
    display_name: str
    description: str
    category: str
    status: str  # running, stopped, degraded, restarting, error, unknown
    desired_replicas: int
    ready_replicas: int
    is_core: bool
    restarts: int
    uptime: str | None = None
    created_at: str | None = None
    pod_name: str | None = None
    node_name: str | None = None
    cpu_usage: str | None = None
    memory_usage: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None
    last_restart: str | None = None
    image: str | None = None
    port: int | None = None
    extra_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KubernetesServiceManager:
    """Manages observation and lifecycle controls for platform services in Kubernetes."""

    def __init__(self, namespace: str | None = None):
        self.namespace = namespace or os.getenv("K8S_NAMESPACE", "lake-of-tears")
        self.sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        self.sa_ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        self.sa_ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
        self._detect_environment()

    def _detect_environment(self) -> None:
        if os.path.exists(self.sa_token_path):
            self.mode = "in_cluster"
            if os.path.exists(self.sa_ns_path):
                try:
                    with open(self.sa_ns_path, encoding="utf-8") as f:
                        ns = f.read().strip()
                        if ns:
                            self.namespace = ns
                except Exception:
                    pass
            host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
            port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
            self.base_url = f"https://{host}:{port}"
        elif shutil.which("kubectl"):
            self.mode = "kubectl_cli"
            self.base_url = ""
        else:
            self.mode = "unsupported"
            self.base_url = ""

    def is_cluster_accessible(self) -> bool:
        if self.mode == "in_cluster":
            return True
        if self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    ["kubectl", "version", "--client"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return res.returncode == 0
            except Exception:
                return False
        return False

    def _get_http_client(self) -> httpx.Client:
        token = ""
        if os.path.exists(self.sa_token_path):
            with open(self.sa_token_path, encoding="utf-8") as f:
                token = f.read().strip()

        return httpx.Client(
            base_url=self.base_url,
            verify=self.sa_ca_path if os.path.exists(self.sa_ca_path) else False,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=10.0,
        )

    def _extract_service_id(self, deployment_name: str) -> str:
        prefix = "lake-of-tears-"
        if deployment_name.startswith(prefix):
            return deployment_name[len(prefix) :]
        return deployment_name

    def _get_deployments(self) -> list[dict[str, Any]]:
        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/apis/apps/v1/namespaces/{self.namespace}/deployments"
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.json().get("items", [])
                logger.warning(
                    "Failed to list deployments via k8s API: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return []
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    [
                        "kubectl",
                        "get",
                        "deployments",
                        "-n",
                        self.namespace,
                        "-o",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    return data.get("items", [])
            except Exception as e:
                logger.warning("Failed to list deployments via kubectl: %s", e)
        return []

    def _get_pods(self) -> list[dict[str, Any]]:
        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/api/v1/namespaces/{self.namespace}/pods"
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.json().get("items", [])
                logger.warning(
                    "Failed to list pods via k8s API: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return []
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    ["kubectl", "get", "pods", "-n", self.namespace, "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    return data.get("items", [])
            except Exception as e:
                logger.warning("Failed to list pods via kubectl: %s", e)
        return []

    def _format_age(self, created_at_str: str | None) -> str | None:
        if not created_at_str:
            return None
        try:
            # Parse ISO 8601 string (e.g. 2026-11-05T04:12:00Z)
            dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            now = datetime.now(UTC)
            diff = now - dt
            seconds = int(diff.total_seconds())
            if seconds < 0:
                return "Just now"
            if seconds < 60:
                return f"{seconds}s"
            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes}m"
            hours = minutes // 60
            if hours < 24:
                return f"{hours}h {minutes % 60}m"
            days = hours // 24
            return f"{days}d {hours % 24}h"
        except Exception:
            return None

    def list_services(self) -> dict[str, Any]:
        """List platform services and their real-time status."""
        deployments = self._get_deployments()
        pods = self._get_pods()

        # Build pods lookup by app or release label
        pods_by_deployment: dict[str, list[dict[str, Any]]] = {}
        for pod in pods:
            labels = pod.get("metadata", {}).get("labels", {})
            app_label = labels.get("app")
            if not app_label:
                # Try finding deployment name from pod prefix
                pod_name = pod.get("metadata", {}).get("name", "")
                parts = pod_name.split("-")
                if len(parts) >= 3:
                    app_label = "-".join(parts[:-2])
            if app_label:
                pods_by_deployment.setdefault(app_label, []).append(pod)

        services: list[ServiceInfo] = []

        # If deployments were found, map each deployment
        if deployments:
            for dep in deployments:
                dep_name = dep.get("metadata", {}).get("name", "")
                srv_id = self._extract_service_id(dep_name)
                spec = dep.get("spec", {})
                status_dict = dep.get("status", {})

                desired_replicas = spec.get("replicas", 0)
                ready_replicas = status_dict.get("readyReplicas", 0)

                meta = SERVICE_METADATA.get(srv_id, {})
                display_name = meta.get("display_name", srv_id.replace("-", " ").title())
                description = meta.get("description", "Platform infrastructure deployment.")
                category = meta.get("category", "General")
                is_core = srv_id in SAFEGUARDED_SERVICES or dep_name in SAFEGUARDED_SERVICES

                # Extract container spec from deployment template
                dep_containers = spec.get("template", {}).get("spec", {}).get("containers", [])
                image = dep_containers[0].get("image") if dep_containers else None
                port = meta.get("port")
                if not port and dep_containers and dep_containers[0].get("ports"):
                    port = dep_containers[0]["ports"][0].get("containerPort")

                # Correlate active pod
                dep_pods = pods_by_deployment.get(dep_name, []) or pods_by_deployment.get(
                    f"lake-of-tears-{srv_id}", []
                )
                active_pod = dep_pods[0] if dep_pods else None

                pod_name = None
                node_name = None
                restarts = 0
                created_at = None
                cpu_limit = None
                memory_limit = None
                last_restart = None

                if active_pod:
                    pod_meta = active_pod.get("metadata", {})
                    pod_spec = active_pod.get("spec", {})
                    pod_status = active_pod.get("status", {})

                    pod_name = pod_meta.get("name")
                    node_name = pod_spec.get("nodeName")
                    created_at = pod_meta.get("creationTimestamp")

                    # Container status
                    container_statuses = pod_status.get("containerStatuses", [])
                    if container_statuses:
                        main_cs = container_statuses[0]
                        restarts = main_cs.get("restartCount", 0)

                    # Resources
                    containers = pod_spec.get("containers", [])
                    if containers:
                        res = containers[0].get("resources", {})
                        limits = res.get("limits", {})
                        cpu_limit = limits.get("cpu")
                        memory_limit = limits.get("memory")
                        if not image:
                            image = containers[0].get("image")

                # Fallback resource limits from deployment template if not found on pod
                if (not cpu_limit or not memory_limit) and dep_containers:
                    dep_limits = dep_containers[0].get("resources", {}).get("limits", {})
                    if not cpu_limit:
                        cpu_limit = dep_limits.get("cpu")
                    if not memory_limit:
                        memory_limit = dep_limits.get("memory")

                # Template annotations for rollout restart time
                template_annotations = (
                    spec.get("template", {}).get("metadata", {}).get("annotations", {})
                )
                last_restart = template_annotations.get("kubectl.kubernetes.io/restartedAt")

                # Determine overall service status
                if desired_replicas == 0:
                    status = "stopped"
                elif ready_replicas == desired_replicas and desired_replicas > 0:
                    status = "running"
                elif ready_replicas < desired_replicas:
                    # Check if pod is restarting or crashloop
                    if active_pod:
                        container_statuses = active_pod.get("status", {}).get(
                            "containerStatuses", []
                        )
                        waiting_state = (
                            container_statuses[0].get("state", {}).get("waiting", {})
                            if container_statuses
                            else {}
                        )
                        reason = waiting_state.get("reason", "")
                        if "CrashLoop" in reason or "BackOff" in reason:
                            status = "error"
                        elif "ContainerCreating" in reason:
                            status = "restarting"
                        else:
                            status = "degraded"
                    else:
                        status = "degraded"
                else:
                    status = "running"

                uptime = self._format_age(created_at)

                services.append(
                    ServiceInfo(
                        id=srv_id,
                        name=dep_name,
                        display_name=display_name,
                        description=description,
                        category=category,
                        status=status,
                        desired_replicas=desired_replicas,
                        ready_replicas=ready_replicas,
                        is_core=is_core,
                        restarts=restarts,
                        uptime=uptime,
                        created_at=created_at,
                        pod_name=pod_name,
                        node_name=node_name,
                        cpu_usage="—",
                        memory_usage="—",
                        cpu_limit=cpu_limit,
                        memory_limit=memory_limit,
                        last_restart=self._format_age(last_restart) if last_restart else None,
                        image=image,
                        port=port,
                    )
                )

        # Sort services: core first, then alphabetically
        services.sort(key=lambda s: (0 if s.is_core else 1, s.display_name))

        connected = bool(deployments) or self.is_cluster_accessible()
        return {
            "cluster_connected": connected,
            "mode": self.mode,
            "namespace": self.namespace,
            "total_services": len(services),
            "healthy_count": sum(1 for s in services if s.status == "running"),
            "services": [s.to_dict() for s in services],
        }

    def _resolve_deployment_name(self, service_name: str) -> str:
        srv = (service_name or "").strip()
        deployments = self._get_deployments()
        dep_names = [d.get("metadata", {}).get("name", "") for d in deployments]

        # Exact match
        if srv in dep_names:
            return srv

        # Try prefix matching
        full_name = f"lake-of-tears-{srv}"
        if full_name in dep_names:
            return full_name

        # Fallback to standard conventions
        for name in dep_names:
            if name.endswith(f"-{srv}") or name == srv:
                return name

        return full_name

    def restart_service(self, service_name: str) -> dict[str, Any]:
        """Trigger a rolling restart of the specified deployment."""
        dep_name = self._resolve_deployment_name(service_name)
        now_iso = datetime.now(UTC).isoformat()

        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{dep_name}"
                patch_payload = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {"kubectl.kubernetes.io/restartedAt": now_iso}
                            }
                        }
                    }
                }
                headers = {"Content-Type": "application/strategic-merge-patch+json"}
                resp = client.patch(url, headers=headers, json=patch_payload)
                if resp.status_code in (200, 201):
                    return {
                        "success": True,
                        "service": service_name,
                        "deployment": dep_name,
                        "action": "restart",
                        "message": f"Service '{service_name}' restart initiated successfully.",
                        "restarted_at": now_iso,
                    }
                raise RuntimeError(f"Kubernetes restart failed ({resp.status_code}): {resp.text}")
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    [
                        "kubectl",
                        "rollout",
                        "restart",
                        f"deployment/{dep_name}",
                        "-n",
                        self.namespace,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    return {
                        "success": True,
                        "service": service_name,
                        "deployment": dep_name,
                        "action": "restart",
                        "message": f"Service '{service_name}' rollout restart triggered.",
                        "restarted_at": now_iso,
                    }
                raise RuntimeError(f"kubectl restart failed: {res.stderr}")
            except Exception as e:
                raise RuntimeError(str(e))

        raise RuntimeError("No Kubernetes management backend available.")

    def stop_service(self, service_name: str) -> dict[str, Any]:
        """Scale deployment replicas to 0. Core services are rejected."""
        srv_id = (service_name or "").lower().strip()
        if srv_id in SAFEGUARDED_SERVICES or f"lake-of-tears-{srv_id}" in SAFEGUARDED_SERVICES:
            raise ValueError(
                f"Core platform service '{service_name}' is safeguarded and cannot be stopped."
            )

        dep_name = self._resolve_deployment_name(service_name)

        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{dep_name}"
                patch_payload = {"spec": {"replicas": 0}}
                headers = {"Content-Type": "application/merge-patch+json"}
                resp = client.patch(url, headers=headers, json=patch_payload)
                if resp.status_code in (200, 201):
                    return {
                        "success": True,
                        "service": service_name,
                        "deployment": dep_name,
                        "action": "stop",
                        "message": f"Service '{service_name}' stopped (replicas scaled to 0).",
                    }
                raise RuntimeError(f"Kubernetes stop failed ({resp.status_code}): {resp.text}")
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    [
                        "kubectl",
                        "scale",
                        f"deployment/{dep_name}",
                        "--replicas=0",
                        "-n",
                        self.namespace,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    return {
                        "success": True,
                        "service": service_name,
                        "deployment": dep_name,
                        "action": "stop",
                        "message": f"Service '{service_name}' scaled to 0 replicas.",
                    }
                raise RuntimeError(f"kubectl scale failed: {res.stderr}")
            except Exception as e:
                raise RuntimeError(str(e))

        raise RuntimeError("No Kubernetes management backend available.")

    def start_service(self, service_name: str) -> dict[str, Any]:
        """Scale deployment replicas back to 1."""
        dep_name = self._resolve_deployment_name(service_name)

        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{dep_name}"
                patch_payload = {"spec": {"replicas": 1}}
                headers = {"Content-Type": "application/merge-patch+json"}
                resp = client.patch(url, headers=headers, json=patch_payload)
                if resp.status_code in (200, 201):
                    return {
                        "success": True,
                        "service": service_name,
                        "deployment": dep_name,
                        "action": "start",
                        "message": f"Service '{service_name}' started (replicas scaled to 1).",
                    }
                raise RuntimeError(f"Kubernetes start failed ({resp.status_code}): {resp.text}")
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    [
                        "kubectl",
                        "scale",
                        f"deployment/{dep_name}",
                        "--replicas=1",
                        "-n",
                        self.namespace,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res.returncode == 0:
                    return {
                        "success": True,
                        "service": service_name,
                        "deployment": dep_name,
                        "action": "start",
                        "message": f"Service '{service_name}' scaled to 1 replica.",
                    }
                raise RuntimeError(f"kubectl scale failed: {res.stderr}")
            except Exception as e:
                raise RuntimeError(str(e))

        raise RuntimeError("No Kubernetes management backend available.")


_global_manager: KubernetesServiceManager | None = None


def get_service_manager() -> KubernetesServiceManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = KubernetesServiceManager()
    return _global_manager
