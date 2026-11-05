from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from auth import decrypt_secret, encrypt_secret
from models import SecretKey, SecretScope
from sqlalchemy.orm import Session

from secrets_manager.base import BaseSecretProvider

logger = logging.getLogger("lake.secrets.internal")


class InternalSecretProvider(BaseSecretProvider):
    """Database-backed internal secret provider using AES-256-GCM / HMAC-SHA256 envelope encryption."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_secret(self, scope: SecretScope, key: str) -> str:
        secret = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id, SecretKey.key == key)
            .first()
        )
        if not secret:
            raise KeyError(f"Secret '{key}' not found in scope '{scope.name}'")

        decrypted = decrypt_secret(secret.encrypted_value)
        return decrypted

    def put_secret(
        self, scope: SecretScope, key: str, value: str, user_id: uuid.UUID | None = None
    ) -> None:
        clean_key = (key or "").strip()
        if not clean_key:
            raise ValueError("Secret key cannot be empty")

        encrypted_val = encrypt_secret(value)
        now = datetime.now(UTC)

        existing = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id, SecretKey.key == clean_key)
            .first()
        )
        if existing:
            existing.encrypted_value = encrypted_val
            existing.version += 1
            existing.updated_at = now
            existing.updated_by = user_id
        else:
            new_key = SecretKey(
                scope_id=scope.id,
                key=clean_key,
                encrypted_value=encrypted_val,
                version=1,
                created_by=user_id,
                updated_by=user_id,
                created_at=now,
                updated_at=now,
            )
            self.db.add(new_key)

        scope.updated_at = now
        self.db.commit()

    def delete_secret(self, scope: SecretScope, key: str) -> bool:
        secret = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id, SecretKey.key == key)
            .first()
        )
        if not secret:
            return False

        self.db.delete(secret)
        scope.updated_at = datetime.now(UTC)
        self.db.commit()
        return True

    def list_keys(self, scope: SecretScope) -> list[str]:
        keys = (
            self.db.query(SecretKey.key)
            .filter(SecretKey.scope_id == scope.id)
            .order_by(SecretKey.key.asc())
            .all()
        )
        return [k[0] for k in keys]

    def test_connection(self, config: dict[str, Any] | None = None) -> tuple[bool, str]:
        try:
            # Test encryption / decryption cycle
            test_payload = "lot-secrets-test-ping"
            enc = encrypt_secret(test_payload)
            dec = decrypt_secret(enc)
            if dec != test_payload:
                return False, "Encryption verification mismatch"
            return True, "Internal encrypted database store is operational"
        except Exception as e:
            logger.exception("InternalSecretProvider test_connection failed")
            return False, f"Internal provider error: {e}"
