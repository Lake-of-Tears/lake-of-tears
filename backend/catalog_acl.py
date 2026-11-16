"""Catalog access resolution shared by the catalog API and the native Iceberg REST catalog."""

from __future__ import annotations

from models import Catalog, CatalogAccess, User, Workspace, WorkspaceShare
from sqlalchemy.orm import Session


def caller_access(
    db: Session,
    catalog: Catalog,
    user: User,
    ws: Workspace | None = None,
) -> str | None:
    """Determine caller's access level ('owner', 'write', 'read', or None) to a catalog.

    When `ws` is provided (active workspace context), access is strictly jailed to that workspace:
    - If active workspace owns the catalog:
      - superadmin -> 'owner'
      - workspace admin/owner -> 'owner'
      - workspace member/write/user -> 'write'
      - workspace viewer/read -> 'read'
    - If active workspace does NOT own the catalog:
      - Checks approved, non-suspended CatalogAccess grant specifically for `ws.id`
      - If no approved grant exists -> returns None (catalog is invisible in this workspace)
      - If grant exists -> grant.mode (e.g. 'read')
    """
    if ws is not None:
        if str(catalog.owner_workspace_id) == str(ws.id):
            if user.role == "superadmin":
                return "owner"
            member = next(
                (m for m in user.workspace_memberships if str(m.workspace_id) == str(ws.id)),
                None,
            )
            if not member:
                share = (
                    db.query(WorkspaceShare)
                    .filter(
                        WorkspaceShare.workspace_id == ws.id,
                        WorkspaceShare.user_id == user.id,
                        WorkspaceShare.status == "approved",
                    )
                    .first()
                )
                if share:
                    return "read"
                return None
            role = member.role.lower() if member.role else "read"
            if role in ("admin", "owner"):
                return "owner"
            if role in ("write", "user", "member"):
                return "write"
            return "read"

        # Cross-workspace: strictly verify approved sharing grant for active workspace and user
        grant = (
            db.query(CatalogAccess)
            .filter(
                CatalogAccess.catalog_id == catalog.id,
                CatalogAccess.workspace_id == ws.id,
                CatalogAccess.requested_by == user.id,
                CatalogAccess.status == "approved",
                CatalogAccess.suspended.is_(False),
            )
            .first()
        )
        return grant.mode if grant else None

    # Fallback when ws is not provided
    if user.role == "superadmin":
        return "owner"
    ws_ids = [str(m.workspace_id) for m in user.workspace_memberships]
    if str(catalog.owner_workspace_id) in ws_ids:
        return "owner"
    grant = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.catalog_id == catalog.id,
            CatalogAccess.workspace_id.in_(ws_ids),
            CatalogAccess.requested_by == user.id,
            CatalogAccess.status == "approved",
            CatalogAccess.suspended.is_(False),
        )
        .first()
    )
    return grant.mode if grant else None
