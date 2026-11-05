from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from secrets_manager.aws_sm import AWSSecretsManagerProvider
from secrets_manager.azure_kv import AzureKeyVaultProvider
from secrets_manager.base import BaseSecretProvider
from secrets_manager.config import get_vault_provider_config
from secrets_manager.internal import InternalSecretProvider


def get_secret_provider(
    backend_type: str,
    db: Session,
    provider_config: dict[str, Any] | None = None,
) -> BaseSecretProvider:
    """Resolve and instantiate the secret provider for a given backend type."""
    normalized = (backend_type or "internal").lower().strip()

    if normalized == "internal":
        return InternalSecretProvider(db)

    if normalized in ("azure_keyvault", "azure"):
        system_conf = get_vault_provider_config(db, "azure_keyvault")
        merged = {**system_conf, **(provider_config or {})}
        return AzureKeyVaultProvider(merged)

    if normalized in ("aws_secrets_manager", "aws"):
        system_conf = get_vault_provider_config(db, "aws_secrets_manager")
        merged = {**system_conf, **(provider_config or {})}
        return AWSSecretsManagerProvider(merged)

    raise NotImplementedError(
        f"Secret provider backend '{backend_type}' is not yet supported. "
        "Supported in Phase 1: 'internal', 'azure_keyvault', 'aws_secrets_manager'."
    )
