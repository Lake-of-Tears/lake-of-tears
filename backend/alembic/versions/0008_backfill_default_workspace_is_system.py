"""backfill is_system on the Default workspace

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16

Migration 0002 backfilled is_system on a fresh install while the workspaces
table was still empty, and workspace creation did not set the flag, so Default
workspaces created afterwards were left deletable (#129).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only when no system workspace exists yet: mark the oldest workspace named 'default'.
    op.execute(
        """
        UPDATE workspaces
        SET is_system = TRUE
        WHERE id = (
            SELECT id FROM workspaces
            WHERE LOWER(name) = 'default'
            ORDER BY created_at ASC
            LIMIT 1
        )
          AND NOT EXISTS (SELECT 1 FROM workspaces WHERE is_system)
        """
    )


def downgrade() -> None:
    # The flag is data, not schema; leaving it set is harmless and not reversible by name.
    pass
