from __future__ import annotations

import logging
from typing import Any

from auth import require_superadmin
from fastapi import APIRouter, Depends, HTTPException, status
from models import User
from services_manager.k8s_manager import get_service_manager

logger = logging.getLogger("lake.backend.services_admin.router")

router = APIRouter()


@router.get("")
def list_platform_services(
    current_user: User = Depends(require_superadmin),
) -> dict[str, Any]:
    """List all Kubernetes platform services, states, uptime, and replicas."""
    mgr = get_service_manager()
    try:
        return mgr.list_services()
    except Exception as e:
        logger.error("Failed to list platform services: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list platform services: {str(e)}",
        )


@router.post("/{service_name}/restart")
def restart_platform_service(
    service_name: str,
    current_user: User = Depends(require_superadmin),
) -> dict[str, Any]:
    """Trigger a rolling restart of the specified platform service deployment."""
    mgr = get_service_manager()
    try:
        return mgr.restart_service(service_name)
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )
    except Exception as e:
        logger.error("Failed to restart service '%s': %s", service_name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to restart service: {str(e)}",
        )


@router.post("/{service_name}/stop")
def stop_platform_service(
    service_name: str,
    current_user: User = Depends(require_superadmin),
) -> dict[str, Any]:
    """Stop the specified service by scaling its deployment to 0 replicas.

    Core services (Backend, Lake UI) are strictly protected against being stopped.
    """
    mgr = get_service_manager()
    try:
        return mgr.stop_service(service_name)
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )
    except Exception as e:
        logger.error("Failed to stop service '%s': %s", service_name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to stop service: {str(e)}",
        )


@router.post("/{service_name}/start")
def start_platform_service(
    service_name: str,
    current_user: User = Depends(require_superadmin),
) -> dict[str, Any]:
    """Start the specified service by scaling its deployment to 1 replica."""
    mgr = get_service_manager()
    try:
        return mgr.start_service(service_name)
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve),
        )
    except Exception as e:
        logger.error("Failed to start service '%s': %s", service_name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start service: {str(e)}",
        )
