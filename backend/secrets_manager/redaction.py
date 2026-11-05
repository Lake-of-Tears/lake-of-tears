from __future__ import annotations

import io
import logging
import re
import threading
from typing import Any

logger = logging.getLogger("lake.secrets.redaction")

_LOCK = threading.RLock()
_REGISTERED_SECRETS: set[str] = set()

# Regex to detect raw LoT envelope encryption tokens (v1:nonce:ciphertext:tag)
LOT_ENVELOPE_PATTERN = re.compile(r"v1:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+")


def register_secret_for_redaction(secret: str | None) -> None:
    """Register a plaintext secret string to be redacted across logs and stdout."""
    if not secret:
        return
    clean = str(secret).strip()
    # Require at least 3 characters to avoid replacing common single characters/numbers
    if len(clean) >= 3:
        with _LOCK:
            _REGISTERED_SECRETS.add(clean)


def unregister_secret_for_redaction(secret: str | None) -> None:
    if not secret:
        return
    with _LOCK:
        _REGISTERED_SECRETS.discard(str(secret).strip())


def clear_registered_secrets() -> None:
    """Clear all registered secrets (useful in tests)."""
    with _LOCK:
        _REGISTERED_SECRETS.clear()


def redact_secrets(
    text: str | None,
    additional_secrets: list[str] | set[str] | None = None,
) -> str:
    """Replace all known and specified secret values with '[REDACTED]' in text."""
    if not text:
        return "" if text is None else text

    with _LOCK:
        all_secrets = set(_REGISTERED_SECRETS)

    if additional_secrets:
        for s in additional_secrets:
            if s and len(str(s).strip()) >= 3:
                all_secrets.add(str(s).strip())

    if not all_secrets and not LOT_ENVELOPE_PATTERN.search(text):
        return text

    redacted = text

    # Sort secrets by length descending so longer matching secrets get replaced first
    sorted_secrets = sorted(all_secrets, key=len, reverse=True)

    for secret in sorted_secrets:
        if secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")

    # Redact raw internal encryption tokens
    redacted = LOT_ENVELOPE_PATTERN.sub("[REDACTED_SECRET_TOKEN]", redacted)

    return redacted


class SecretRedactionFilter(logging.Filter):
    """Logging filter that sanitizes record messages and exceptions."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (redact_secrets(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a for a in record.args
                )
        return True


class RedactingStream(io.TextIOBase):
    """A stream wrapper that intercepts writes and replaces registered secrets with [REDACTED]."""

    def __init__(self, target: Any) -> None:
        self.target = target

    def write(self, s: str) -> int:
        sanitized = redact_secrets(s)
        return self.target.write(sanitized)

    def flush(self) -> None:
        if hasattr(self.target, "flush"):
            self.target.flush()
