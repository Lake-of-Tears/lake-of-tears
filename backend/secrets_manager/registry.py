from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from secrets_manager.aws_sm import AWSSecretsManagerProvider
from secrets_manager.azure_kv import AzureKeyVaultProvider
from secrets_manager.base import BaseSecretProvider
from secrets_manager.config import get_vault_provider_config
from secrets_manager.gcp_sm import GCPSecretManagerProvider
from secrets_manager.hashicorp_vault import HashiCorpVaultProvider
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

    if normalized in ("azure_keyvault", "azure", "azure_kv"):
        system_conf = get_vault_provider_config(db, "azure_keyvault")
        merged = {**system_conf, **(provider_config or {})}
        return AzureKeyVaultProvider(merged)

    if normalized in ("aws_secrets_manager", "aws", "aws_sm"):
        system_conf = get_vault_provider_config(db, "aws_secrets_manager")
        merged = {**system_conf, **(provider_config or {})}
        return AWSSecretsManagerProvider(merged)

    if normalized in ("hashicorp_vault", "hashicorp", "vault"):
        system_conf = get_vault_provider_config(db, "hashicorp_vault")
        merged = {**system_conf, **(provider_config or {})}
        return HashiCorpVaultProvider(merged)

    if normalized in ("gcp_secret_manager", "gcp", "google"):
        system_conf = get_vault_provider_config(db, "gcp_secret_manager")
        merged = {**system_conf, **(provider_config or {})}
        return GCPSecretManagerProvider(merged)

    raise NotImplementedError(
        f"Secret provider backend '{backend_type}' is not supported. "
        "Supported: 'internal', 'azure_keyvault', 'aws_secrets_manager', 'hashicorp_vault', 'gcp_secret_manager'."
    )
