from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from models import SecretScope

from secrets_manager.base import BaseSecretProvider

logger = logging.getLogger("lake.secrets.hashicorp_vault")


class HashiCorpVaultProvider(BaseSecretProvider):
    """HashiCorp Vault secret provider with KV v1 and v2 engines, supporting Token,

    AppRole, and Kubernetes service account authentication, custom namespaces, and key rotation.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.vault_url = self.config.get("vault_url", "").strip()
        self.auth_mode = self.config.get("auth_mode", "token").lower().strip()
        self.token = self.config.get("token", "").strip()
        self.role_id = self.config.get("role_id", "").strip()
        self.secret_id = self.config.get("secret_id", "").strip()
        self.approle_mount_point = (
            self.config.get("approle_mount_point", "approle").strip() or "approle"
        )
        self.k8s_role = self.config.get("k8s_role", "").strip()
        self.k8s_jwt = self.config.get("k8s_jwt", "").strip()
        self.k8s_jwt_path = (
            self.config.get(
                "k8s_jwt_path", "/var/run/secrets/kubernetes.io/serviceaccount/token"
            ).strip()
            or "/var/run/secrets/kubernetes.io/serviceaccount/token"
        )
        self.k8s_mount_point = (
            self.config.get("k8s_mount_point", "kubernetes").strip() or "kubernetes"
        )
        self.namespace = self.config.get("namespace", "").strip() or None
        self.mount_point = self.config.get("mount_point", "secret").strip() or "secret"
        self.kv_version = int(self.config.get("kv_version", 2))
        self.default_prefix = self.config.get("prefix", "").strip()
        self.verify = self.config.get("verify", True)
        self._client = None

    def _create_client(self, config_override: dict[str, Any] | None = None):
        conf = {**self.config, **(config_override or {})}
        vault_url = conf.get("vault_url", self.vault_url).strip()
        if not vault_url:
            raise ValueError("HashiCorp Vault URL is not configured")

        namespace = conf.get("namespace", self.namespace)
        if isinstance(namespace, str):
            namespace = namespace.strip() or None

        verify = conf.get("verify", self.verify)
        if isinstance(verify, str):
            if verify.lower() in ("false", "0", "no"):
                verify = False
            elif verify.lower() in ("true", "1", "yes"):
                verify = True

        import hvac

        client = hvac.Client(url=vault_url, namespace=namespace, verify=verify)

        auth_mode = conf.get("auth_mode", self.auth_mode).lower().strip()
        role_id = conf.get("role_id", self.role_id).strip()
        secret_id = conf.get("secret_id", self.secret_id).strip()
        token = conf.get("token", self.token).strip()
        k8s_role = conf.get("k8s_role", self.k8s_role).strip()

        if auth_mode == "approle" or (role_id and secret_id):
            approle_mount = (
                conf.get("approle_mount_point", self.approle_mount_point).strip() or "approle"
            )
            client.auth.approle.login(
                role_id=role_id,
                secret_id=secret_id,
                mount_point=approle_mount,
            )
        elif auth_mode == "kubernetes" or k8s_role:
            jwt = conf.get("k8s_jwt", self.k8s_jwt).strip()
            if not jwt:
                jwt_path = (
                    conf.get("k8s_jwt_path", self.k8s_jwt_path).strip()
                    or "/var/run/secrets/kubernetes.io/serviceaccount/token"
                )
                try:
                    with open(jwt_path) as f:
                        jwt = f.read().strip()
                except Exception as e:
                    raise ValueError(
                        f"Failed to read Kubernetes service account token from '{jwt_path}': {e}"
                    )
            k8s_mount = conf.get("k8s_mount_point", self.k8s_mount_point).strip() or "kubernetes"
            client.auth.kubernetes.login(
                role=k8s_role,
                jwt=jwt,
                mount_point=k8s_mount,
            )
        elif token:
            client.token = token

        return client

    def _get_client(self, config_override: dict[str, Any] | None = None):
        if config_override:
            return self._create_client(config_override)
        if not self._client:
            self._client = self._create_client()
        return self._client

    def _resolve_scope_config(self, scope: SecretScope) -> dict[str, Any]:
        scope_conf = scope.provider_config or {}
        kv_ver = scope_conf.get("kv_version", self.kv_version)
        try:
            kv_ver_int = int(kv_ver)
        except (ValueError, TypeError):
            kv_ver_int = 2

        return {
            "mount_point": (scope_conf.get("mount_point") or self.mount_point or "secret").strip(),
            "kv_version": kv_ver_int,
            "prefix": scope_conf.get("prefix", self.default_prefix).strip(),
            "namespace": (scope_conf.get("namespace") or self.namespace or "").strip() or None,
        }

    def _resolve_path(self, scope: SecretScope, key: str | None = None) -> str:
        scope_conf = self._resolve_scope_config(scope)
        prefix = scope_conf["prefix"]
        parts = []
        if prefix:
            parts.append(prefix.strip("/"))
        parts.append(scope.name.strip("/"))
        if key:
            parts.append(key.strip("/"))
        return "/".join(parts)

    def get_secret(self, scope: SecretScope, key: str) -> str:
        client = self._get_client()
        scope_conf = self._resolve_scope_config(scope)
        mount_point = scope_conf["mount_point"]
        kv_version = scope_conf["kv_version"]

        from hvac.exceptions import InvalidPath

        # 1. Try reading key path: f"{prefix}/{scope.name}/{key}"
        key_path = self._resolve_path(scope, key)
        try:
            if kv_version == 1:
                res = client.secrets.kv.v1.read_secret(path=key_path, mount_point=mount_point)
                data = res.get("data", {}) if isinstance(res, dict) else {}
            else:
                res = client.secrets.kv.v2.read_secret_version(
                    path=key_path, mount_point=mount_point
                )
                data = res.get("data", {}).get("data", {}) if isinstance(res, dict) else {}

            if "value" in data:
                return str(data["value"])
            if key in data:
                return str(data[key])
            if len(data) == 1:
                return str(next(iter(data.values())))
            if data:
                return json.dumps(data)
        except (InvalidPath, KeyError):
            pass
        except Exception as e:
            if not (hasattr(e, "status_code") and getattr(e, "status_code") == 404):
                raise

        # 2. Try reading scope path: f"{prefix}/{scope.name}" where dictionary holds multiple keys
        scope_path = self._resolve_path(scope)
        try:
            if kv_version == 1:
                res = client.secrets.kv.v1.read_secret(path=scope_path, mount_point=mount_point)
                data = res.get("data", {}) if isinstance(res, dict) else {}
            else:
                res = client.secrets.kv.v2.read_secret_version(
                    path=scope_path, mount_point=mount_point
                )
                data = res.get("data", {}).get("data", {}) if isinstance(res, dict) else {}

            if key in data:
                return str(data[key])
        except (InvalidPath, KeyError):
            pass
        except Exception as e:
            if not (hasattr(e, "status_code") and getattr(e, "status_code") == 404):
                raise

        raise KeyError(
            f"Secret '{key}' not found in HashiCorp Vault at path '{key_path}' (mount: '{mount_point}')"
        )

    def put_secret(
        self, scope: SecretScope, key: str, value: str, user_id: uuid.UUID | None = None
    ) -> None:
        client = self._get_client()
        scope_conf = self._resolve_scope_config(scope)
        mount_point = scope_conf["mount_point"]
        kv_version = scope_conf["kv_version"]
        key_path = self._resolve_path(scope, key)

        secret_payload = {"value": value, key: value}

        if kv_version == 1:
            client.secrets.kv.v1.create_or_update_secret(
                path=key_path,
                secret=secret_payload,
                mount_point=mount_point,
            )
        else:
            client.secrets.kv.v2.create_or_update_secret(
                path=key_path,
                secret=secret_payload,
                mount_point=mount_point,
            )

    def delete_secret(self, scope: SecretScope, key: str) -> bool:
        client = self._get_client()
        scope_conf = self._resolve_scope_config(scope)
        mount_point = scope_conf["mount_point"]
        kv_version = scope_conf["kv_version"]
        key_path = self._resolve_path(scope, key)

        from hvac.exceptions import InvalidPath

        deleted = False

        # Attempt deleting at key_path
        try:
            if kv_version == 1:
                client.secrets.kv.v1.delete_secret(path=key_path, mount_point=mount_point)
            else:
                client.secrets.kv.v2.delete_metadata_and_all_versions(
                    path=key_path, mount_point=mount_point
                )
            deleted = True
        except (InvalidPath, KeyError):
            pass
        except Exception as e:
            if not (hasattr(e, "status_code") and getattr(e, "status_code") == 404):
                raise

        # Also check dictionary at scope_path
        scope_path = self._resolve_path(scope)
        try:
            if kv_version == 1:
                res = client.secrets.kv.v1.read_secret(path=scope_path, mount_point=mount_point)
                data = dict(res.get("data", {})) if isinstance(res, dict) else {}
                if key in data:
                    del data[key]
                    if data:
                        client.secrets.kv.v1.create_or_update_secret(
                            path=scope_path, secret=data, mount_point=mount_point
                        )
                    else:
                        client.secrets.kv.v1.delete_secret(path=scope_path, mount_point=mount_point)
                    deleted = True
            else:
                res = client.secrets.kv.v2.read_secret_version(
                    path=scope_path, mount_point=mount_point
                )
                data = dict(res.get("data", {}).get("data", {})) if isinstance(res, dict) else {}
                if key in data:
                    del data[key]
                    if data:
                        client.secrets.kv.v2.create_or_update_secret(
                            path=scope_path, secret=data, mount_point=mount_point
                        )
                    else:
                        client.secrets.kv.v2.delete_metadata_and_all_versions(
                            path=scope_path, mount_point=mount_point
                        )
                    deleted = True
        except (InvalidPath, KeyError):
            pass
        except Exception as e:
            if not (hasattr(e, "status_code") and getattr(e, "status_code") == 404):
                raise

        return deleted

    def list_keys(self, scope: SecretScope) -> list[str]:
        client = self._get_client()
        scope_conf = self._resolve_scope_config(scope)
        mount_point = scope_conf["mount_point"]
        kv_version = scope_conf["kv_version"]
        scope_path = self._resolve_path(scope)

        from hvac.exceptions import InvalidPath

        found_keys: set[str] = set()

        # 1. List child secrets under scope_path folder
        try:
            if kv_version == 1:
                res = client.secrets.kv.v1.list_secrets(path=scope_path, mount_point=mount_point)
            else:
                res = client.secrets.kv.v2.list_secrets(path=scope_path, mount_point=mount_point)
            raw_keys = res.get("data", {}).get("keys", []) if isinstance(res, dict) else []
            for k in raw_keys:
                found_keys.add(k.rstrip("/"))
        except (InvalidPath, KeyError):
            pass
        except Exception as e:
            if not (hasattr(e, "status_code") and getattr(e, "status_code") == 404):
                logger.warning("Error listing Vault keys under %s: %s", scope_path, e)

        # 2. Check if scope_path itself is a secret dictionary
        try:
            if kv_version == 1:
                res = client.secrets.kv.v1.read_secret(path=scope_path, mount_point=mount_point)
                data = res.get("data", {}) if isinstance(res, dict) else {}
            else:
                res = client.secrets.kv.v2.read_secret_version(
                    path=scope_path, mount_point=mount_point
                )
                data = res.get("data", {}).get("data", {}) if isinstance(res, dict) else {}
            for k in data.keys():
                if k != "value":
                    found_keys.add(k)
        except (InvalidPath, KeyError):
            pass
        except Exception as e:
            if not (hasattr(e, "status_code") and getattr(e, "status_code") == 404):
                logger.warning("Error reading scope secret %s: %s", scope_path, e)

        return sorted(list(found_keys))

    def test_connection(self, config: dict[str, Any] | None = None) -> tuple[bool, str]:
        test_conf = {**self.config, **(config or {})}
        vault_url = test_conf.get("vault_url", "").strip()
        if not vault_url:
            return False, "HashiCorp Vault URL is missing or empty"

        try:
            client = self._create_client(test_conf)
            if not client.is_authenticated():
                return (
                    False,
                    "HashiCorp Vault authentication failed (invalid token, AppRole, or Kubernetes credentials)",
                )

            try:
                if client.sys.is_sealed():
                    return False, f"HashiCorp Vault at {vault_url} is currently sealed"
            except Exception:
                pass

            mount = test_conf.get("mount_point", self.mount_point) or "secret"
            return (
                True,
                f"Successfully connected and authenticated to HashiCorp Vault at {vault_url} (mount: {mount})",
            )
        except Exception as e:
            logger.warning("HashiCorp Vault test_connection failed: %s", e)
            return False, f"HashiCorp Vault connection failed: {e}"
