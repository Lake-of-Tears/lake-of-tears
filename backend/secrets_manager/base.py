from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from models import SecretScope


class BaseSecretProvider(ABC):
    """Abstract secret provider interface mirroring Databricks Secret Store."""

    @abstractmethod
    def get_secret(self, scope: SecretScope, key: str) -> str:
        """Retrieve the plaintext secret value for key in scope.

        Raises KeyError if key does not exist.
        """
        pass

    @abstractmethod
    def put_secret(
        self, scope: SecretScope, key: str, value: str, user_id: uuid.UUID | None = None
    ) -> None:
        """Store or rotate a secret key in scope."""
        pass

    @abstractmethod
    def delete_secret(self, scope: SecretScope, key: str) -> bool:
        """Delete a secret key from scope. Returns True if deleted, False if not found."""
        pass

    @abstractmethod
    def list_keys(self, scope: SecretScope) -> list[str]:
        """List all secret key names present in scope. Never returns secret values."""
        pass

    @abstractmethod
    def test_connection(self, config: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Verify provider reachability and credentials."""
        pass
