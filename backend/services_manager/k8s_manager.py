from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
    "duckdb": {
        "display_name": "DuckDB Analytical Engine",
        "description": "Embedded in-process analytical SQL OLAP query engine.",
        "category": "Analytics",
        "port": None,
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
    active_connections: int | None = None
    latency_ms: float | None = None
    error: str | None = None
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

    def _get_ingresses(self) -> list[dict[str, Any]]:
        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/apis/networking.k8s.io/v1/namespaces/{self.namespace}/ingresses"
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.json().get("items", [])
                logger.warning(
                    "Failed to list ingresses via k8s API: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return []
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    ["kubectl", "get", "ingress", "-n", self.namespace, "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    return data.get("items", [])
            except Exception as e:
                logger.warning("Failed to list ingresses via kubectl: %s", e)
        return []

    def _get_pvcs(self) -> list[dict[str, Any]]:
        if self.mode == "in_cluster":
            with self._get_http_client() as client:
                url = f"/api/v1/namespaces/{self.namespace}/persistentvolumeclaims"
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.json().get("items", [])
                logger.warning(
                    "Failed to list PVCs via k8s API: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return []
        elif self.mode == "kubectl_cli":
            try:
                res = subprocess.run(
                    ["kubectl", "get", "pvc", "-n", self.namespace, "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    return data.get("items", [])
            except Exception as e:
                logger.warning("Failed to list PVCs via kubectl: %s", e)
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

    def _probe_duckdb(self) -> tuple[str, float | None, str | None]:
        """Actively probe DuckDB embedded SQL engine."""
        try:
            import duckdb

            t0 = time.perf_counter()
            with duckdb.connect() as con:
                con.execute("SELECT 1")
            latency = (time.perf_counter() - t0) * 1000.0
            return "running", round(latency, 2), None
        except Exception as e:
            logger.warning("DuckDB health probe failed: %s", e)
            return "error", None, str(e)

    def _probe_postgres(self) -> tuple[str, float | None, str | None, int | None]:
        """Actively probe PostgreSQL connection pool."""
        try:
            import sqlalchemy
            from database import engine

            t0 = time.perf_counter()
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("SELECT 1"))
            latency = (time.perf_counter() - t0) * 1000.0
            active = getattr(getattr(engine, "pool", None), "checkedout", lambda: 0)()
            return "running", round(latency, 2), None, active
        except Exception as e:
            return "stopped", None, str(e), None

    def _probe_http_service(
        self, url: str, timeout: float = 1.0
    ) -> tuple[str, float | None, str | None]:
        """Perform a low-latency HTTP health probe against an internal service."""
        try:
            t0 = time.perf_counter()
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(url)
            latency = (time.perf_counter() - t0) * 1000.0
            if resp.status_code < 500:
                return "running", round(latency, 2), None
            return "degraded", round(latency, 2), f"HTTP {resp.status_code}"
        except Exception as e:
            return "stopped", None, str(e)

    def _load_toon_data(self) -> dict[str, Any]:
        """Load active cluster TOON configuration file if present."""
        candidates: list[Path] = []
        if os.getenv("TOON_CONFIG_PATH"):
            candidates.append(Path(os.getenv("TOON_CONFIG_PATH")))

        try:
            repo_root = Path(__file__).resolve().parents[2]
            candidates.append(repo_root / "lake-of-tears.local.toon")
            candidates.append(repo_root / "lake-of-tears.example.toon")
        except Exception:
            pass

        candidates.append(Path("lake-of-tears.local.toon"))
        candidates.append(Path("lake-of-tears.example.toon"))

        for path in candidates:
            if path.exists() and path.is_file():
                try:
                    from toon import parse_toon

                    with open(path, encoding="utf-8") as f:
                        return parse_toon(f.read())
                except Exception as e:
                    logger.warning("Failed to parse TOON configuration at %s: %s", path, e)
        return {}

    def get_cluster_status(self) -> dict[str, Any]:
        """Inspect real-time cluster health, pod allocations, and service latency."""
        base_services = self.list_services()
        k8s_services: dict[str, dict[str, Any]] = {
            s["id"]: s for s in base_services.get("services", [])
        }

        all_ids = [
            "duckdb",
            "backend",
            "postgres",
            "minio",
            "airflow",
            "jupyter",
            "superset",
            "lakeui",
        ]
        enriched_services: dict[str, dict[str, Any]] = {}

        for srv_id in all_ids:
            meta = SERVICE_METADATA.get(srv_id, {})
            k8s_info = k8s_services.get(srv_id, {})

            display_name = meta.get("display_name", srv_id.title())
            category = meta.get("category", "General")
            port = meta.get("port", k8s_info.get("port"))
            pod_name = k8s_info.get("pod_name")
            node_name = k8s_info.get("node_name")
            restarts = k8s_info.get("restarts", 0)
            uptime = k8s_info.get("uptime")
            created_at = k8s_info.get("created_at")
            cpu_usage = k8s_info.get("cpu_usage")
            memory_usage = k8s_info.get("memory_usage")
            cpu_limit = k8s_info.get("cpu_limit")
            memory_limit = k8s_info.get("memory_limit")
            extra = dict(k8s_info.get("extra_details", {}))

            st = k8s_info.get("status")
            latency_ms: float | None = None
            error_msg: str | None = None
            active_conns: int | None = None

            if srv_id == "duckdb":
                p_status, latency_ms, error_msg = self._probe_duckdb()
                final_status = p_status
                active_conns = 1 if p_status == "running" else 0
            elif srv_id == "backend":
                final_status = "running"
                latency_ms = 0.5
                active_conns = 1
            elif srv_id == "postgres":
                p_status, p_latency, p_err, p_active = self._probe_postgres()
                latency_ms = p_latency
                active_conns = p_active
                error_msg = p_err
                if st in ("running", "degraded"):
                    final_status = st
                else:
                    final_status = p_status
            elif srv_id == "minio":
                raw_minio = os.getenv("MINIO_ENDPOINT", "localhost:9000")
                if not raw_minio.startswith("http://") and not raw_minio.startswith("https://"):
                    probe_url = f"http://{raw_minio}/minio/health/live"
                else:
                    probe_url = f"{raw_minio.rstrip('/')}/minio/health/live"
                p_status, p_latency, p_err = self._probe_http_service(probe_url)
                latency_ms = p_latency
                error_msg = p_err
                if st in ("running", "degraded"):
                    final_status = st
                else:
                    final_status = p_status
            elif srv_id == "airflow":
                raw_af = os.getenv("AIRFLOW_URL", "http://localhost:8080")
                probe_url = f"{raw_af.rstrip('/')}/health"
                p_status, p_latency, p_err = self._probe_http_service(probe_url)
                latency_ms = p_latency
                error_msg = p_err
                if st in ("running", "degraded"):
                    final_status = st
                else:
                    final_status = p_status
            elif srv_id == "jupyter":
                raw_jup = os.getenv("JUPYTER_URL", "http://localhost:8888")
                probe_url = f"{raw_jup.rstrip('/')}/api/status"
                p_status, p_latency, p_err = self._probe_http_service(probe_url)
                latency_ms = p_latency
                error_msg = p_err
                if st in ("running", "degraded"):
                    final_status = st
                else:
                    final_status = p_status
            elif srv_id == "superset":
                raw_ss = os.getenv("SUPERSET_URL", "http://localhost:8088")
                probe_url = f"{raw_ss.rstrip('/')}/health"
                p_status, p_latency, p_err = self._probe_http_service(probe_url)
                latency_ms = p_latency
                error_msg = p_err
                if st in ("running", "degraded"):
                    final_status = st
                else:
                    final_status = p_status
            else:
                final_status = st or "running"

            enriched_services[srv_id] = {
                "name": srv_id,
                "display_name": display_name,
                "status": final_status,
                "category": category,
                "port": port,
                "pod_name": pod_name,
                "node_name": node_name,
                "restarts": restarts,
                "uptime": uptime,
                "created_at": created_at,
                "cpu_usage": cpu_usage,
                "memory_usage": memory_usage,
                "cpu_limit": cpu_limit,
                "memory_limit": memory_limit,
                "active_connections": active_conns,
                "latency_ms": latency_ms,
                "error": error_msg,
                "extra_details": extra,
            }

        healthy_count = sum(1 for s in enriched_services.values() if s["status"] == "running")
        total_services = len(enriched_services)

        core_healthy = all(
            enriched_services.get(cid, {}).get("status") == "running"
            for cid in ("backend", "duckdb")
        )
        if core_healthy and healthy_count >= total_services - 2:
            overall_status = "healthy"
        elif core_healthy:
            overall_status = "degraded"
        else:
            overall_status = "down"

        return {
            "status": overall_status,
            "cluster_connected": base_services.get("cluster_connected", False),
            "mode": self.mode,
            "namespace": self.namespace,
            "timestamp": datetime.now(UTC),
            "total_services": total_services,
            "healthy_count": healthy_count,
            "services": enriched_services,
        }

    def get_cluster_topology(self) -> dict[str, Any]:
        """Inspect storage volume paths, active ingress routes, and namespace configuration."""
        toon_data = self._load_toon_data()
        ingress_cfg = toon_data.get("ingress", {})
        global_cfg = toon_data.get("global", {})
        minio_cfg = toon_data.get("minio", {})

        # 1. Resolve Ingress Routes
        ingress_routes: list[dict[str, Any]] = []
        k8s_ingresses = self._get_ingresses()

        if k8s_ingresses:
            for ing in k8s_ingresses:
                spec = ing.get("spec", {})
                tls_hosts = set()
                for tls_item in spec.get("tls", []):
                    for h in tls_item.get("hosts", []):
                        tls_hosts.add(h)
                for rule in spec.get("rules", []):
                    host = rule.get("host", "")
                    for path_item in rule.get("http", {}).get("paths", []):
                        path = path_item.get("path", "/")
                        backend_svc = (
                            path_item.get("backend", {}).get("service", {}).get("name", "unknown")
                        )
                        svc_id = self._extract_service_id(backend_svc)
                        tls_enabled = host in tls_hosts
                        scheme = "https" if tls_enabled else "http"
                        ingress_routes.append(
                            {
                                "service": svc_id,
                                "url": f"{scheme}://{host}{path}",
                                "host": host,
                                "path": path,
                                "tls_enabled": tls_enabled,
                            }
                        )

        if not ingress_routes:
            base_host = ingress_cfg.get("host") or os.getenv("INGRESS_HOST") or "localhost"
            minio_console = (
                ingress_cfg.get("minioConsoleHost")
                or os.getenv("MINIO_CONSOLE_HOST")
                or f"minio.{base_host}"
            )
            minio_api = (
                ingress_cfg.get("minioApiHost") or os.getenv("MINIO_API_HOST") or f"s3.{base_host}"
            )
            superset_host = (
                ingress_cfg.get("supersetHost")
                or os.getenv("SUPERSET_HOST")
                or f"superset.{base_host}"
            )

            ingress_routes = [
                {
                    "service": "lakeui",
                    "url": f"http://{base_host}/",
                    "host": base_host,
                    "path": "/",
                    "tls_enabled": False,
                },
                {
                    "service": "airflow",
                    "url": f"http://{base_host}/airflow/",
                    "host": base_host,
                    "path": "/airflow/",
                    "tls_enabled": False,
                },
                {
                    "service": "minio-console",
                    "url": f"http://{minio_console}/",
                    "host": minio_console,
                    "path": "/",
                    "tls_enabled": False,
                },
                {
                    "service": "minio-api",
                    "url": f"http://{minio_api}/",
                    "host": minio_api,
                    "path": "/",
                    "tls_enabled": False,
                },
                {
                    "service": "superset",
                    "url": f"http://{superset_host}/",
                    "host": superset_host,
                    "path": "/",
                    "tls_enabled": False,
                },
                {
                    "service": "jupyter",
                    "url": f"http://{base_host}/jupyter/",
                    "host": base_host,
                    "path": "/jupyter/",
                    "tls_enabled": False,
                },
            ]

        # 2. Resolve Storage Volumes
        storage_volumes: list[dict[str, Any]] = []
        k8s_pvcs = self._get_pvcs()

        if k8s_pvcs:
            for pvc in k8s_pvcs:
                meta = pvc.get("metadata", {})
                spec = pvc.get("spec", {})
                pvc_name = meta.get("name", "")
                resource_requests = spec.get("resources", {}).get("requests", {})
                size = resource_requests.get("storage")
                sc = spec.get("storageClassName")
                status = pvc.get("status", {}).get("phase", "Bound")

                mount = "/data"
                if "postgres" in pvc_name:
                    mount = "/var/lib/postgresql/data"
                elif "airflow" in pvc_name:
                    mount = "/opt/airflow/dags"
                elif "jupyter" in pvc_name:
                    mount = "/home/jovyan/work"
                elif "catalog" in pvc_name:
                    mount = "/var/lake/catalog"

                storage_volumes.append(
                    {
                        "name": pvc_name,
                        "pvc_name": pvc_name,
                        "mount_path": mount,
                        "size": size,
                        "storage_class": sc,
                        "status": status,
                    }
                )

        if not storage_volumes:
            storage_class = global_cfg.get("storageClass") or "local-path"
            minio_size = minio_cfg.get("persistence", {}).get("size", "200Gi")
            catalog_dir = os.getenv("LAKE_CATALOG_DIR", "/var/lake/catalog")

            storage_volumes = [
                {
                    "name": "minio-data",
                    "pvc_name": "minio-data-pvc",
                    "mount_path": "/data",
                    "size": minio_size,
                    "storage_class": storage_class,
                    "status": "Active",
                },
                {
                    "name": "postgres-data",
                    "pvc_name": "postgres-data-pvc",
                    "mount_path": "/var/lib/postgresql/data",
                    "size": "20Gi",
                    "storage_class": storage_class,
                    "status": "Active",
                },
                {
                    "name": "lakehouse-catalog",
                    "pvc_name": "lakehouse-catalog-pvc",
                    "mount_path": catalog_dir,
                    "size": "500Gi",
                    "storage_class": storage_class,
                    "status": "Active",
                },
                {
                    "name": "airflow-dags",
                    "pvc_name": "airflow-dags-pvc",
                    "mount_path": "/opt/airflow/dags",
                    "size": "10Gi",
                    "storage_class": storage_class,
                    "status": "Active",
                },
                {
                    "name": "jupyter-workspaces",
                    "pvc_name": "jupyter-workspaces-pvc",
                    "mount_path": "/home/jovyan/work",
                    "size": "50Gi",
                    "storage_class": storage_class,
                    "status": "Active",
                },
            ]

        return {
            "namespace": self.namespace,
            "mode": self.mode,
            "ingress_routes": ingress_routes,
            "storage_volumes": storage_volumes,
            "raw_config": {
                "storageClass": global_cfg.get("storageClass", "local-path"),
                "ingressHost": ingress_cfg.get("host", "localhost"),
            },
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
