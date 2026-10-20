"""workspace member roles read write admin

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Migrate existing 'user' roles (or any legacy role outside 'admin', 'write', 'read') to 'read'
    op.execute(
        sa.text(
            "UPDATE workspace_members SET role = 'read' "
            "WHERE role = 'user' OR role NOT IN ('admin', 'write', 'read')"
        )
    )
    # Alter column server_default to 'read'
    op.alter_column(
        "workspace_members",
        "role",
        server_default="read",
        existing_type=sa.String(20),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Revert roles: 'write' -> 'admin', 'read' -> 'user'
    op.execute(sa.text("UPDATE workspace_members SET role = 'admin' WHERE role = 'write'"))
    op.execute(sa.text("UPDATE workspace_members SET role = 'user' WHERE role = 'read'"))
    # Alter column server_default back to 'user'
    op.alter_column(
        "workspace_members",
        "role",
        server_default="user",
        existing_type=sa.String(20),
        existing_nullable=False,
    )
