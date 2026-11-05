from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from secrets_manager.base import BaseSecretProvider
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

    # Future cloud providers (Issue #381 / #384) will register here
    raise NotImplementedError(
        f"Secret provider backend '{backend_type}' is not yet configured or supported. "
        "Supported in Phase 1: 'internal'."
    )
