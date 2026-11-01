"""create personal_access_tokens table

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
    )
    op.create_index(
        op.f("ix_personal_access_tokens_user_id"),
        "personal_access_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_token_prefix"),
        "personal_access_tokens",
        ["token_prefix"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_token_hash"),
        "personal_access_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_workspace_id"),
        "personal_access_tokens",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_expires_at"),
        "personal_access_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_is_revoked"),
        "personal_access_tokens",
        ["is_revoked"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_personal_access_tokens_is_revoked"), table_name="personal_access_tokens")
    op.drop_index(op.f("ix_personal_access_tokens_expires_at"), table_name="personal_access_tokens")
    op.drop_index(op.f("ix_personal_access_tokens_workspace_id"), table_name="personal_access_tokens")
    op.drop_index(op.f("ix_personal_access_tokens_token_hash"), table_name="personal_access_tokens")
    op.drop_index(op.f("ix_personal_access_tokens_token_prefix"), table_name="personal_access_tokens")
    op.drop_index(op.f("ix_personal_access_tokens_user_id"), table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
