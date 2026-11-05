from __future__ import annotations

import logging
import uuid
from typing import Any

from models import SecretScope

from secrets_manager.base import BaseSecretProvider

logger = logging.getLogger("lake.secrets.azure_kv")


class AzureKeyVaultProvider(BaseSecretProvider):
    """Azure Key Vault secret provider with DefaultAzureCredential and Service Principal auth."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.vault_url = self.config.get("vault_url", "").strip()
        self.tenant_id = self.config.get("tenant_id", "").strip()
        self.client_id = self.config.get("client_id", "").strip()
        self.client_secret = self.config.get("client_secret", "").strip()
        self.auth_mode = self.config.get("auth_mode", "default").lower().strip()
        self._client = None

    def _get_client(self, override_url: str | None = None):
        if self._client and not override_url:
            return self._client

        vault_url = override_url or self.vault_url
        if not vault_url:
            raise ValueError("Azure Key Vault URL is not configured")

        from azure.keyvault.secrets import SecretClient

        if self.auth_mode == "service_principal" or (
            self.tenant_id and self.client_id and self.client_secret
        ):
            from azure.identity import ClientSecretCredential

            cred = ClientSecretCredential(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
        else:
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()

        client = SecretClient(vault_url=vault_url, credential=cred)
        if not override_url:
            self._client = client
        return client

    def _resolve_vault_url(self, scope: SecretScope) -> str:
        scope_conf = scope.provider_config or {}
        return (scope_conf.get("vault_url") or self.vault_url or "").strip()

    def get_secret(self, scope: SecretScope, key: str) -> str:
        vault_url = self._resolve_vault_url(scope)
        client = self._get_client(vault_url)
        from azure.core.exceptions import ResourceNotFoundError

        try:
            secret = client.get_secret(name=key)
            return secret.value or ""
        except ResourceNotFoundError:
            raise KeyError(f"Secret '{key}' not found in Azure Key Vault ({vault_url})")

    def put_secret(
        self, scope: SecretScope, key: str, value: str, user_id: uuid.UUID | None = None
    ) -> None:
        vault_url = self._resolve_vault_url(scope)
        client = self._get_client(vault_url)
        client.set_secret(name=key, value=value)

    def delete_secret(self, scope: SecretScope, key: str) -> bool:
        vault_url = self._resolve_vault_url(scope)
        client = self._get_client(vault_url)
        from azure.core.exceptions import ResourceNotFoundError

        try:
            client.begin_delete_secret(name=key)
            return True
        except ResourceNotFoundError:
            return False

    def list_keys(self, scope: SecretScope) -> list[str]:
        vault_url = self._resolve_vault_url(scope)
        client = self._get_client(vault_url)
        try:
            properties = client.list_properties_of_secrets()
            return [p.name for p in properties if getattr(p, "enabled", True)]
        except Exception as e:
            logger.error("Failed to list Azure Key Vault secrets: %s", e)
            raise

    def test_connection(self, config: dict[str, Any] | None = None) -> tuple[bool, str]:
        test_conf = {**self.config, **(config or {})}
        vault_url = test_conf.get("vault_url", "").strip()
        if not vault_url:
            return False, "Azure Key Vault URL is missing or empty"

        try:
            client = self._get_client(vault_url)
            # Verify authentication and vault reachability
            iterator = iter(client.list_properties_of_secrets())
            next(iterator, None)
            return True, f"Successfully connected to Azure Key Vault at {vault_url}"
        except Exception as e:
            logger.warning("Azure Key Vault test_connection failed: %s", e)
            return False, f"Azure Key Vault connection failed: {e}"
