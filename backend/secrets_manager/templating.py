from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import HTTPException
from models import User
from sqlalchemy.orm import Session

from secrets_manager.redaction import register_secret_for_redaction
from secrets_manager.service import SecretManagerService

logger = logging.getLogger("lake.secrets.templating")

# Matches {{secrets/scope/key}}, {{ secrets.scope.key }}, {{secrets:scope:key}}
DOUBLE_CURLY_PATTERN = re.compile(
    r"\{\{\s*secrets[/.:]([a-zA-Z0-9_\-\.]+)[/.:]([a-zA-Z0-9_\-\.]+)\s*\}\}"
)

# Matches ${secrets:scope:key}, ${secrets/scope/key}
SHELL_STYLE_PATTERN = re.compile(
    r"\$\{\s*secrets[:/.]([a-zA-Z0-9_\-\.]+)[:/.]([a-zA-Z0-9_\-\.]+)\s*\}"
)


def has_secret_templates(val: Any) -> bool:
    """Check if a string, dict, or list contains unresolved secret placeholders."""
    if isinstance(val, str):
        return bool(DOUBLE_CURLY_PATTERN.search(val) or SHELL_STYLE_PATTERN.search(val))
    if isinstance(val, dict):
        return any(has_secret_templates(k) or has_secret_templates(v) for k, v in val.items())
    if isinstance(val, list):
        return any(has_secret_templates(item) for item in val)
    return False


def _resolve_string(
    text: str,
    workspace_id: uuid.UUID,
    db: Session,
    user: User | None = None,
    workspace_role: str | None = None,
) -> str:
    svc = SecretManagerService(db)

    def _replace_match(match: re.Match) -> str:
        scope_name = match.group(1)
        key_name = match.group(2)

        try:
            scope = svc.get_scope(workspace_id, scope_name)
        except HTTPException as exc:
            raise KeyError(
                f"Secret template resolution error: Scope '{scope_name}' not found in workspace ({exc.detail})"
            ) from exc

        if user:
            svc.require_permission(scope, user, workspace_role, "READ")

        val, _, _ = svc.get_secret_value(scope, key_name)
        # Register resolved secret value with redaction filter immediately
        register_secret_for_redaction(val)
        logger.info(
            "Secret template interpolated: scope=%s, key=%s, workspace=%s",
            scope_name,
            key_name,
            workspace_id,
        )
        return val

    # Replace both {{secrets/...}} and ${secrets:...}
    res = DOUBLE_CURLY_PATTERN.sub(_replace_match, text)
    res = SHELL_STYLE_PATTERN.sub(_replace_match, res)
    return res


def resolve_secret_templates(
    target: Any,
    workspace_id: uuid.UUID,
    db: Session,
    user: User | None = None,
    workspace_role: str | None = None,
) -> Any:
    """Recursively resolve secret template placeholders in strings, dicts, or lists."""
    if isinstance(target, str):
        return _resolve_string(
            target,
            workspace_id=workspace_id,
            db=db,
            user=user,
            workspace_role=workspace_role,
        )

    if isinstance(target, dict):
        return {
            k: resolve_secret_templates(
                v,
                workspace_id=workspace_id,
                db=db,
                user=user,
                workspace_role=workspace_role,
            )
            for k, v in target.items()
        }

    if isinstance(target, list):
        return [
            resolve_secret_templates(
                item,
                workspace_id=workspace_id,
                db=db,
                user=user,
                workspace_role=workspace_role,
            )
            for item in target
        ]

    return target
