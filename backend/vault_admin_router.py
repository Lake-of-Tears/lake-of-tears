from __future__ import annotations

import logging
from typing import Any

from auth import require_superadmin
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, status
from models import User
from schemas import (
    VaultProviderConfigUpdate,
    VaultProviderStatusResponse,
    VaultTestConnectionRequest,
    VaultTestConnectionResponse,
)
from secrets_manager.config import (
    get_vault_provider_config,
    mask_vault_provider_config,
    set_vault_provider_config,
)
from secrets_manager.registry import get_secret_provider
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.vault_admin.router")

router = APIRouter()

PROVIDER_METADATA = {
    "internal": {
        "display_name": "Internal Encrypted Database",
        "supported": True,
    },
    "azure_keyvault": {
        "display_name": "Azure Key Vault",
        "supported": True,
    },
    "aws_secrets_manager": {
        "display_name": "AWS Secrets Manager",
        "supported": True,
    },
    "hashicorp_vault": {
        "display_name": "HashiCorp Vault",
        "supported": True,
    },
    "gcp_secret_manager": {
        "display_name": "Google Cloud Secret Manager",
        "supported": True,
    },
}


def _normalize_provider(provider: str) -> str:
    norm = (provider or "").lower().strip()
    if norm in ("azure", "azure_keyvault"):
        return "azure_keyvault"
    if norm in ("aws", "aws_secrets_manager"):
        return "aws_secrets_manager"
    if norm in ("vault", "hashicorp", "hashicorp_vault"):
        return "hashicorp_vault"
    if norm in ("gcp", "google", "gcp_secret_manager"):
        return "gcp_secret_manager"
    if norm == "internal":
        return "internal"
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Unknown vault provider '{provider}'. Supported: internal, azure_keyvault, aws_secrets_manager, hashicorp_vault, gcp_secret_manager",
    )


@router.get("/scopes")
def list_all_scopes(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> list[dict[str, Any]]:
    """List all secret scopes across all workspaces for administrative audit."""
    from models import SecretScope

    scopes = db.query(SecretScope).order_by(SecretScope.name.asc()).all()
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "workspace_id": str(s.workspace_id) if s.workspace_id else None,
            "workspace_name": s.workspace.name if s.workspace else "Global",
            "backend_type": s.backend_type,
            "key_count": len(s.keys),
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in scopes
    ]


@router.get("", response_model=list[VaultProviderStatusResponse])
def list_vault_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> list[VaultProviderStatusResponse]:
    """List all supported key vault providers and their configuration status (secrets masked)."""
    results = []

    # Internal
    results.append(
        VaultProviderStatusResponse(
            provider="internal",
            display_name="Internal Encrypted Database",
            is_configured=True,
            config={"encryption": "AES-256-GCM / HMAC-SHA256", "status": "active"},
        )
    )

    # Azure Key Vault
    az_conf = get_vault_provider_config(db, "azure_keyvault")
    results.append(
        VaultProviderStatusResponse(
            provider="azure_keyvault",
            display_name="Azure Key Vault",
            is_configured=bool(az_conf.get("vault_url")),
            config=mask_vault_provider_config("azure_keyvault", az_conf),
        )
    )

    # AWS Secrets Manager
    aws_conf = get_vault_provider_config(db, "aws_secrets_manager")
    results.append(
        VaultProviderStatusResponse(
            provider="aws_secrets_manager",
            display_name="AWS Secrets Manager",
            is_configured=bool(aws_conf.get("region_name") or aws_conf.get("aws_access_key_id")),
            config=mask_vault_provider_config("aws_secrets_manager", aws_conf),
        )
    )

    # HashiCorp Vault
    hashi_conf = get_vault_provider_config(db, "hashicorp_vault")
    results.append(
        VaultProviderStatusResponse(
            provider="hashicorp_vault",
            display_name="HashiCorp Vault",
            is_configured=bool(hashi_conf.get("vault_url")),
            config=mask_vault_provider_config("hashicorp_vault", hashi_conf),
        )
    )

    # Google Cloud Secret Manager
    gcp_conf = get_vault_provider_config(db, "gcp_secret_manager")
    results.append(
        VaultProviderStatusResponse(
            provider="gcp_secret_manager",
            display_name="Google Cloud Secret Manager",
            is_configured=bool(gcp_conf.get("project_id")),
            config=mask_vault_provider_config("gcp_secret_manager", gcp_conf),
        )
    )

    return results


@router.get("/{provider}", response_model=VaultProviderStatusResponse)
def get_vault_provider(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> VaultProviderStatusResponse:
    norm_provider = _normalize_provider(provider)
    if norm_provider == "internal":
        return VaultProviderStatusResponse(
            provider="internal",
            display_name="Internal Encrypted Database",
            is_configured=True,
            config={"encryption": "AES-256-GCM / HMAC-SHA256", "status": "active"},
        )

    conf = get_vault_provider_config(db, norm_provider)
    is_cfg = False
    if norm_provider == "azure_keyvault":
        is_cfg = bool(conf.get("vault_url"))
    elif norm_provider == "aws_secrets_manager":
        is_cfg = bool(conf.get("region_name") or conf.get("aws_access_key_id"))
    elif norm_provider == "hashicorp_vault":
        is_cfg = bool(conf.get("vault_url"))
    elif norm_provider == "gcp_secret_manager":
        is_cfg = bool(conf.get("project_id"))

    return VaultProviderStatusResponse(
        provider=norm_provider,
        display_name=PROVIDER_METADATA[norm_provider]["display_name"],
        is_configured=is_cfg,
        config=mask_vault_provider_config(norm_provider, conf),
    )


@router.put("/{provider}", response_model=VaultProviderStatusResponse)
@router.post("/{provider}/config", response_model=VaultProviderStatusResponse)
def update_vault_provider_config(
    provider: str,
    payload: VaultProviderConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> VaultProviderStatusResponse:
    norm_provider = _normalize_provider(provider)
    if norm_provider == "internal":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Internal provider configuration is managed automatically by Lake of Tears.",
        )

    existing = get_vault_provider_config(db, norm_provider)
    updated = dict(payload.config)

    # Preserve existing secrets if payload value is masked or unchanged
    for secret_field in (
        "client_secret",
        "aws_secret_access_key",
        "aws_session_token",
        "token",
        "secret_id",
        "k8s_jwt",
        "credentials_json",
        "service_account_json",
    ):
        if secret_field in updated:
            raw_val = str(updated[secret_field]).strip()
            if raw_val.startswith("••••••••") or not raw_val:
                updated[secret_field] = existing.get(secret_field, "")

    set_vault_provider_config(db, norm_provider, updated, user_id=current_user.id)
    saved = get_vault_provider_config(db, norm_provider)

    is_cfg = False
    if norm_provider == "azure_keyvault":
        is_cfg = bool(saved.get("vault_url"))
    elif norm_provider == "aws_secrets_manager":
        is_cfg = bool(saved.get("region_name") or saved.get("aws_access_key_id"))
    elif norm_provider == "hashicorp_vault":
        is_cfg = bool(saved.get("vault_url"))
    elif norm_provider == "gcp_secret_manager":
        is_cfg = bool(saved.get("project_id"))

    return VaultProviderStatusResponse(
        provider=norm_provider,
        display_name=PROVIDER_METADATA[norm_provider]["display_name"],
        is_configured=is_cfg,
        config=mask_vault_provider_config(norm_provider, saved),
    )


@router.post("/{provider}/test", response_model=VaultTestConnectionResponse)
def test_vault_provider_connection(
    provider: str,
    payload: VaultTestConnectionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> VaultTestConnectionResponse:
    norm_provider = _normalize_provider(provider)
    incoming_conf = payload.config if payload and payload.config else {}

    existing_conf = get_vault_provider_config(db, norm_provider)
    merged_conf = {**existing_conf, **incoming_conf}

    # Handle masked fields during testing
    for secret_field in (
        "client_secret",
        "aws_secret_access_key",
        "aws_session_token",
        "token",
        "secret_id",
        "k8s_jwt",
        "credentials_json",
        "service_account_json",
    ):
        if secret_field in merged_conf and str(merged_conf[secret_field]).startswith("••••••••"):
            merged_conf[secret_field] = existing_conf.get(secret_field, "")

    try:
        prov = get_secret_provider(norm_provider, db, merged_conf)
        ok, msg = prov.test_connection(merged_conf)
        return VaultTestConnectionResponse(success=ok, message=msg)
    except Exception as e:
        logger.warning("Vault test_connection error for %s: %s", norm_provider, e)
        return VaultTestConnectionResponse(
            success=False,
            message=f"Connection test failed: {e}",
        )
