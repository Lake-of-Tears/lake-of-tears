"""create user_workspace_preferences table for personal catalog ordering and view preferences

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_workspace_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "workspace_id", name="uq_user_workspace_preferences"),
    )
    op.create_index(
        op.f("ix_user_workspace_preferences_user_id"),
        "user_workspace_preferences",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_workspace_preferences_workspace_id"),
        "user_workspace_preferences",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_user_workspace_preferences_workspace_id"),
        table_name="user_workspace_preferences",
    )
    op.drop_index(
        op.f("ix_user_workspace_preferences_user_id"),
        table_name="user_workspace_preferences",
    )
    op.drop_table("user_workspace_preferences")
