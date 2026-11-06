from __future__ import annotations

import logging

from auth import get_current_user
from fastapi import APIRouter, Depends, HTTPException, status
from models import User
from schemas import ClusterStatusResponse, ClusterTopologyResponse
from services_manager.k8s_manager import get_service_manager

logger = logging.getLogger("lake.backend.cluster.router")

router = APIRouter()


@router.get(
    "/status",
    response_model=ClusterStatusResponse,
    summary="Get cluster service health and compute capacity",
)
def get_cluster_status(
    current_user: User = Depends(get_current_user),
) -> ClusterStatusResponse:
    """Return real-time cluster health, pod allocations, and service latency."""
    mgr = get_service_manager()
    try:
        data = mgr.get_cluster_status()
        return ClusterStatusResponse(**data)
    except Exception as e:
        logger.error("Failed to retrieve cluster status: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve cluster status: {str(e)}",
        )


@router.get(
    "/topology",
    response_model=ClusterTopologyResponse,
    summary="Get cluster topology, ingress routes, and storage volume paths",
)
def get_cluster_topology(
    current_user: User = Depends(get_current_user),
) -> ClusterTopologyResponse:
    """Return active ingress routes, storage volume paths, and cluster namespace configuration."""
    mgr = get_service_manager()
    try:
        data = mgr.get_cluster_topology()
        return ClusterTopologyResponse(**data)
    except Exception as e:
        logger.error("Failed to retrieve cluster topology: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve cluster topology: {str(e)}",
        )
