from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from auth import decrypt_secret, encrypt_secret
from models import SystemSetting
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.secrets.config")

SUPPORTED_PROVIDERS = ("internal", "azure_keyvault", "aws_secrets_manager")


def _get_setting_key(provider: str) -> str:
    norm = provider.lower().strip()
    if norm in ("azure", "azure_keyvault"):
        return "vault_azure_config"
    if norm in ("aws", "aws_secrets_manager"):
        return "vault_aws_config"
    return f"vault_{norm}_config"


def get_vault_provider_config(db: Session, provider: str) -> dict[str, Any]:
    """Retrieve and decrypt the system-wide configuration for a vault provider."""
    key = _get_setting_key(provider)
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if not setting or not setting.value_str:
        return {}

    try:
        decrypted = decrypt_secret(setting.value_str)
        if not decrypted:
            return {}
        return json.loads(decrypted)
    except Exception as e:
        logger.error("Failed to decrypt config for provider '%s': %s", provider, e)
        return {}


def set_vault_provider_config(
    db: Session,
    provider: str,
    config: dict[str, Any],
    user_id: uuid.UUID | None = None,
) -> None:
    """Encrypt and store the system-wide configuration for a vault provider in system_settings."""
    key = _get_setting_key(provider)
    clean_config = config or {}
    now = datetime.now(UTC)

    json_str = json.dumps(clean_config)
    encrypted_val = encrypt_secret(json_str)

    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if setting:
        setting.value_str = encrypted_val
        setting.updated_at = now
        setting.updated_by = user_id
    else:
        db.add(
            SystemSetting(
                key=key,
                value=None,
                value_str=encrypted_val,
                updated_at=now,
                updated_by=user_id,
            )
        )
    db.commit()


def mask_vault_provider_config(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized copy of provider configuration with sensitive secrets masked."""
    masked = dict(config)
    sensitive_keys = {
        "client_secret",
        "aws_secret_access_key",
        "aws_session_token",
    }
    for k in sensitive_keys:
        if k in masked and masked[k]:
            val = str(masked[k])
            if len(val) > 4:
                masked[k] = "••••••••" + val[-4:]
            else:
                masked[k] = "••••••••"
    return masked
