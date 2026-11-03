"""add webhook_secret to workspaces

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-02
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "webhook_secret",
            sa.String(128),
            nullable=True,
        ),
    )
    conn = op.get_bind()
    results = conn.execute(
        sa.text("SELECT id FROM workspaces WHERE webhook_secret IS NULL")
    ).fetchall()
    for row in results:
        sec = f"whsec_{secrets.token_hex(24)}"
        conn.execute(
            sa.text("UPDATE workspaces SET webhook_secret = :sec WHERE id = :id"),
            {"sec": sec, "id": row[0]},
        )


def downgrade() -> None:
    op.drop_column("workspaces", "webhook_secret")
