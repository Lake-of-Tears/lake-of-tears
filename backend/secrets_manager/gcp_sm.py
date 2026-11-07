from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

from models import SecretScope

from secrets_manager.base import BaseSecretProvider

logger = logging.getLogger("lake.secrets.gcp_sm")


class GCPSecretManagerProvider(BaseSecretProvider):
    """Google Cloud Secret Manager provider supporting Workload Identity / ADC,

    Service Account Key JSON, and project secret version management.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.project_id = self.config.get("project_id", "").strip()
        self.auth_mode = self.config.get("auth_mode", "default").lower().strip()
        self.credentials_json = self.config.get("credentials_json") or self.config.get(
            "service_account_json"
        )
        self.credentials_path = self.config.get("credentials_path") or self.config.get(
            "service_account_file"
        )
        self.default_prefix = self.config.get("prefix", "").strip()
        self._client = None

    def _create_client(self, config_override: dict[str, Any] | None = None):
        conf = {**self.config, **(config_override or {})}
        creds_json = conf.get("credentials_json") or conf.get("service_account_json")
        creds_path = conf.get("credentials_path") or conf.get("service_account_file")

        from google.cloud import secretmanager

        if creds_json:
            from google.oauth2 import service_account

            info = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
            creds = service_account.Credentials.from_service_account_info(info)
            return secretmanager.SecretManagerServiceClient(credentials=creds)

        if creds_path:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(creds_path)
            return secretmanager.SecretManagerServiceClient(credentials=creds)

        # Default Application Credentials (Workload Identity / GKE / Cloud Run)
        return secretmanager.SecretManagerServiceClient()

    def _get_client(self, config_override: dict[str, Any] | None = None):
        if config_override:
            return self._create_client(config_override)
        if not self._client:
            self._client = self._create_client()
        return self._client

    def _resolve_project_id(
        self,
        scope: SecretScope | None = None,
        config_override: dict[str, Any] | None = None,
    ) -> str:
        if config_override and config_override.get("project_id"):
            val = str(config_override["project_id"]).strip()
            if val:
                return val

        if scope and scope.provider_config and scope.provider_config.get("project_id"):
            val = str(scope.provider_config["project_id"]).strip()
            if val:
                return val

        if self.project_id:
            return self.project_id

        # Try to infer project_id from service account json
        creds_json = (
            (config_override or {}).get("credentials_json")
            or (config_override or {}).get("service_account_json")
            or self.credentials_json
        )
        if creds_json:
            try:
                info = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
                if isinstance(info, dict) and info.get("project_id"):
                    return str(info["project_id"]).strip()
            except Exception:
                pass

        env_proj = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
        if env_proj:
            return env_proj.strip()

        raise ValueError("Google Cloud Project ID is not configured")

    def _resolve_secret_id(self, scope: SecretScope, key: str) -> str:
        scope_conf = scope.provider_config or {}
        prefix = (scope_conf.get("prefix") or self.default_prefix).strip()
        if prefix:
            prefix_clean = prefix.rstrip("/").rstrip("_")
            base = f"{prefix_clean}__{key}"
        else:
            base = f"{scope.name}__{key}"

        # GCP secret IDs must match ^[a-zA-Z0-9_-]+$ with max length 255
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", base)
        return sanitized[:255]

    def _strip_secret_id(self, scope: SecretScope, secret_id: str) -> str | None:
        scope_conf = scope.provider_config or {}
        prefix = (scope_conf.get("prefix") or self.default_prefix).strip()
        lead = f"{prefix.rstrip('/').rstrip('_')}__" if prefix else f"{scope.name}__"
        sanitized_lead = re.sub(r"[^a-zA-Z0-9_-]", "_", lead)
        if secret_id.startswith(sanitized_lead):
            return secret_id[len(sanitized_lead) :]
        return None

    def get_secret(self, scope: SecretScope, key: str) -> str:
        from google.api_core.exceptions import NotFound

        project_id = self._resolve_project_id(scope)
        secret_id = self._resolve_secret_id(scope, key)
        client = self._get_client()

        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        try:
            response = client.access_secret_version(request={"name": name})
            return response.payload.data.decode("utf-8")
        except NotFound:
            raise KeyError(
                f"Secret '{key}' ({secret_id}) not found in Google Cloud Secret Manager (project: {project_id})"
            )

    def put_secret(
        self, scope: SecretScope, key: str, value: str, user_id: uuid.UUID | None = None
    ) -> None:
        from google.api_core.exceptions import NotFound

        project_id = self._resolve_project_id(scope)
        secret_id = self._resolve_secret_id(scope, key)
        client = self._get_client()

        parent = f"projects/{project_id}"
        secret_name = f"{parent}/secrets/{secret_id}"
        payload_data = value.encode("utf-8")

        try:
            client.add_secret_version(
                request={
                    "parent": secret_name,
                    "payload": {"data": payload_data},
                }
            )
        except NotFound:
            # Secret resource does not exist yet; create it then add initial version
            sanitized_scope = re.sub(r"[^a-zA-Z0-9_-]", "_", scope.name.lower())[:63]
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {
                        "replication": {"automatic": {}},
                        "labels": {
                            "lake_scope": sanitized_scope,
                        },
                    },
                }
            )
            client.add_secret_version(
                request={
                    "parent": secret_name,
                    "payload": {"data": payload_data},
                }
            )

    def delete_secret(self, scope: SecretScope, key: str) -> bool:
        from google.api_core.exceptions import NotFound

        project_id = self._resolve_project_id(scope)
        secret_id = self._resolve_secret_id(scope, key)
        client = self._get_client()

        secret_name = f"projects/{project_id}/secrets/{secret_id}"
        try:
            client.delete_secret(request={"name": secret_name})
            return True
        except NotFound:
            return False

    def list_keys(self, scope: SecretScope) -> list[str]:
        project_id = self._resolve_project_id(scope)
        client = self._get_client()

        parent = f"projects/{project_id}"
        keys: list[str] = []
        sanitized_scope = re.sub(r"[^a-zA-Z0-9_-]", "_", scope.name.lower())[:63]

        try:
            for secret in client.list_secrets(request={"parent": parent}):
                sec_id = secret.name.split("/")[-1]
                k = self._strip_secret_id(scope, sec_id)
                if k:
                    keys.append(k)
                elif secret.labels and secret.labels.get("lake_scope") == sanitized_scope:
                    if sec_id.startswith(f"{sanitized_scope}__"):
                        keys.append(sec_id[len(f"{sanitized_scope}__") :])
                    else:
                        keys.append(sec_id)
        except Exception as e:
            logger.warning("Failed to list secrets in GCP project %s: %s", project_id, e)
            raise

        return sorted(list(set(keys)))

    def test_connection(self, config: dict[str, Any] | None = None) -> tuple[bool, str]:
        test_conf = {**self.config, **(config or {})}
        try:
            project_id = self._resolve_project_id(config_override=test_conf)
        except Exception as e:
            return False, str(e)

        try:
            client = self._get_client(test_conf)
            parent = f"projects/{project_id}"
            pager = client.list_secrets(request={"parent": parent, "page_size": 1})
            next(iter(pager), None)
            return (
                True,
                f"Successfully connected to Google Cloud Secret Manager (project: {project_id})",
            )
        except Exception as e:
            logger.warning("GCP Secret Manager test_connection failed: %s", e)
            return False, f"Google Cloud Secret Manager connection failed: {e}"
