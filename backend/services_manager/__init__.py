"""Lake of Tears Service & Process Management Package.

Provides Kubernetes in-cluster and host-level container service observability,
metrics reporting, and lifecycle control (Start, Stop, Restart).
"""

from .k8s_manager import KubernetesServiceManager, ServiceInfo, get_service_manager

__all__ = ["KubernetesServiceManager", "ServiceInfo", "get_service_manager"]
