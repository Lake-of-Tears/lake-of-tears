from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from models import SecretAcl, SecretKey, SecretScope, User
from sqlalchemy.orm import Session

from secrets_manager.registry import get_secret_provider

logger = logging.getLogger("lake.secrets.service")

PERMISSION_LEVELS: dict[str, int] = {
    "READ": 1,
    "WRITE": 2,
    "MANAGE": 3,
}

SCOPE_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


class SecretManagerService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def resolve_user_permission(
        self,
        scope: SecretScope,
        user: User,
        workspace_role: str | None = None,
    ) -> str | None:
        """Calculate the highest permission level for a user on a given scope."""
        if getattr(user, "role", None) == "superadmin":
            return "MANAGE"

        if workspace_role == "admin":
            return "MANAGE"

        if scope.created_by is not None and scope.created_by == user.id:
            return "MANAGE"

        highest_level = 0
        user_id_str = str(user.id)

        acls = self.db.query(SecretAcl).filter(SecretAcl.scope_id == scope.id).all()
        for acl in acls:
            match = False
            if acl.principal_type == "user" and acl.principal_id == user_id_str:
                match = True
            elif (
                acl.principal_type == "role"
                and workspace_role
                and acl.principal_id == workspace_role
            ):
                match = True

            if match:
                level = PERMISSION_LEVELS.get(acl.permission.upper(), 0)
                if level > highest_level:
                    highest_level = level

        for perm, lvl in PERMISSION_LEVELS.items():
            if lvl == highest_level:
                return perm
        return None

    def require_permission(
        self,
        scope: SecretScope,
        user: User,
        workspace_role: str | None,
        required_permission: str,
    ) -> str:
        """Enforce that the caller has at least the required permission on the scope."""
        req_level = PERMISSION_LEVELS.get(required_permission.upper())
        if not req_level:
            raise ValueError(f"Unknown required permission: {required_permission}")

        user_perm = self.resolve_user_permission(scope, user, workspace_role)
        curr_level = PERMISSION_LEVELS.get(user_perm or "", 0)

        if curr_level < req_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. '{required_permission}' permission required on scope '{scope.name}'.",
            )
        return user_perm or required_permission

    def list_scopes(
        self,
        workspace_id: uuid.UUID,
        user: User,
        workspace_role: str | None = None,
    ) -> list[tuple[SecretScope, str, int]]:
        """List all secret scopes in a workspace accessible to the caller, with permission and key count."""
        scopes = (
            self.db.query(SecretScope)
            .filter(SecretScope.workspace_id == workspace_id)
            .order_by(SecretScope.name.asc())
            .all()
        )

        results = []
        for scope in scopes:
            perm = self.resolve_user_permission(scope, user, workspace_role)
            if perm is not None:
                key_count = self.db.query(SecretKey).filter(SecretKey.scope_id == scope.id).count()
                results.append((scope, perm, key_count))
        return results

    def get_scope(
        self,
        workspace_id: uuid.UUID,
        scope_identifier: str,
    ) -> SecretScope:
        """Find a scope by UUID or by name within a workspace."""
        scope = None
        try:
            scope_uuid = uuid.UUID(str(scope_identifier))
            scope = (
                self.db.query(SecretScope)
                .filter(
                    SecretScope.id == scope_uuid,
                    SecretScope.workspace_id == workspace_id,
                )
                .first()
            )
        except (ValueError, TypeError):
            pass

        if not scope:
            scope = (
                self.db.query(SecretScope)
                .filter(
                    SecretScope.name == scope_identifier,
                    SecretScope.workspace_id == workspace_id,
                )
                .first()
            )

        if not scope:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Secret scope '{scope_identifier}' not found in workspace",
            )
        return scope

    def create_scope(
        self,
        workspace_id: uuid.UUID,
        name: str,
        backend_type: str = "internal",
        provider_config: dict[str, Any] | None = None,
        user: User | None = None,
    ) -> SecretScope:
        clean_name = (name or "").strip()
        if not clean_name or not SCOPE_NAME_REGEX.match(clean_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Scope name must be non-empty and contain only letters, numbers, underscores, periods, and hyphens.",
            )

        existing = (
            self.db.query(SecretScope)
            .filter(
                SecretScope.workspace_id == workspace_id,
                SecretScope.name == clean_name,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Secret scope '{clean_name}' already exists in this workspace",
            )

        now = datetime.now(UTC)
        scope = SecretScope(
            workspace_id=workspace_id,
            name=clean_name,
            backend_type=backend_type or "internal",
            provider_config=provider_config or {},
            created_by=user.id if user else None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(scope)
        self.db.flush()

        # Grant MANAGE ACL to the creator explicitly
        if user:
            acl = SecretAcl(
                scope_id=scope.id,
                principal_type="user",
                principal_id=str(user.id),
                permission="MANAGE",
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
            self.db.add(acl)

        self.db.commit()
        self.db.refresh(scope)
        return scope

    def delete_scope(self, scope: SecretScope) -> None:
        self.db.delete(scope)
        self.db.commit()

    def list_keys_in_scope(self, scope: SecretScope) -> list[SecretKey]:
        provider = get_secret_provider(scope.backend_type, self.db, scope.provider_config)
        try:
            key_names = provider.list_keys(scope)
        except Exception as e:
            logger.warning("Failed to list keys from provider for scope %s: %s", scope.name, e)
            key_names = []

        records = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id)
            .order_by(SecretKey.key.asc())
            .all()
        )
        rec_map = {r.key: r for r in records}
        now = datetime.now(UTC)

        results = []
        for kn in key_names:
            if kn in rec_map:
                results.append(rec_map[kn])
            else:
                results.append(
                    SecretKey(
                        scope_id=scope.id,
                        key=kn,
                        encrypted_value="[EXTERNAL_MANAGED]",
                        version=1,
                        created_at=scope.created_at or now,
                        updated_at=scope.updated_at or now,
                    )
                )
        return results or records

    def get_secret_value(self, scope: SecretScope, key: str) -> tuple[str, int, datetime]:
        provider = get_secret_provider(scope.backend_type, self.db, scope.provider_config)
        try:
            plaintext = provider.get_secret(scope, key)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Secret key '{key}' not found in scope '{scope.name}'",
            )

        secret_record = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id, SecretKey.key == key)
            .first()
        )
        version = secret_record.version if secret_record else 1
        updated_at = secret_record.updated_at if secret_record else datetime.now(UTC)
        return plaintext, version, updated_at

    def put_secret_value(
        self,
        scope: SecretScope,
        key: str,
        value: str,
        user: User,
    ) -> SecretKey:
        clean_key = (key or "").strip()
        if not clean_key or not SCOPE_NAME_REGEX.match(clean_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Secret key name must contain only letters, numbers, underscores, periods, and hyphens.",
            )

        provider = get_secret_provider(scope.backend_type, self.db, scope.provider_config)
        provider.put_secret(scope, clean_key, value, user_id=user.id)

        secret_record = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id, SecretKey.key == clean_key)
            .first()
        )
        now = datetime.now(UTC)
        if not secret_record:
            secret_record = SecretKey(
                scope_id=scope.id,
                key=clean_key,
                encrypted_value="[EXTERNAL_MANAGED]" if scope.backend_type != "internal" else "",
                version=1,
                created_by=user.id,
                updated_by=user.id,
                created_at=now,
                updated_at=now,
            )
            self.db.add(secret_record)
            self.db.commit()
            self.db.refresh(secret_record)
        elif scope.backend_type != "internal":
            secret_record.version += 1
            secret_record.updated_at = now
            secret_record.updated_by = user.id
            self.db.commit()
            self.db.refresh(secret_record)

        return secret_record

    def delete_secret_value(self, scope: SecretScope, key: str) -> bool:
        provider = get_secret_provider(scope.backend_type, self.db, scope.provider_config)
        deleted = provider.delete_secret(scope, key)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Secret key '{key}' not found in scope '{scope.name}'",
            )
        rec = (
            self.db.query(SecretKey)
            .filter(SecretKey.scope_id == scope.id, SecretKey.key == key)
            .first()
        )
        if rec:
            self.db.delete(rec)
            self.db.commit()
        return True

    def list_acls(self, scope: SecretScope) -> list[SecretAcl]:
        return (
            self.db.query(SecretAcl)
            .filter(SecretAcl.scope_id == scope.id)
            .order_by(SecretAcl.created_at.asc())
            .all()
        )

    def set_acl(
        self,
        scope: SecretScope,
        principal_type: str,
        principal_id: str,
        permission: str,
        user: User,
    ) -> SecretAcl:
        perm_upper = permission.upper()
        if perm_upper not in PERMISSION_LEVELS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid permission: '{permission}'. Must be READ, WRITE, or MANAGE.",
            )

        ptype_clean = principal_type.lower().strip()
        if ptype_clean not in ("user", "role"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid principal_type. Must be 'user' or 'role'.",
            )

        now = datetime.now(UTC)
        acl = (
            self.db.query(SecretAcl)
            .filter(
                SecretAcl.scope_id == scope.id,
                SecretAcl.principal_type == ptype_clean,
                SecretAcl.principal_id == principal_id,
            )
            .first()
        )
        if acl:
            acl.permission = perm_upper
            acl.updated_at = now
        else:
            acl = SecretAcl(
                scope_id=scope.id,
                principal_type=ptype_clean,
                principal_id=principal_id,
                permission=perm_upper,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
            self.db.add(acl)

        self.db.commit()
        self.db.refresh(acl)
        return acl

    def delete_acl(self, scope: SecretScope, acl_id: uuid.UUID) -> None:
        acl = (
            self.db.query(SecretAcl)
            .filter(SecretAcl.scope_id == scope.id, SecretAcl.id == acl_id)
            .first()
        )
        if not acl:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Secret ACL entry not found",
            )
        self.db.delete(acl)
        self.db.commit()
