"""create secret_audit_logs table for secret write/delete audit trail

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_name", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bundle_name", sa.String(length=255), nullable=True),
        sa.Column("pipeline_name", sa.String(length=255), nullable=True),
        sa.Column("task_name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scope_id"], ["secret_scopes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        op.f("ix_secret_audit_logs_workspace_id"),
        "secret_audit_logs",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_secret_audit_logs_scope_id"),
        "secret_audit_logs",
        ["scope_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_secret_audit_logs_key"),
        "secret_audit_logs",
        ["key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_secret_audit_logs_created_at"),
        "secret_audit_logs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_secret_audit_logs_created_at"), table_name="secret_audit_logs")
    op.drop_index(op.f("ix_secret_audit_logs_key"), table_name="secret_audit_logs")
    op.drop_index(op.f("ix_secret_audit_logs_scope_id"), table_name="secret_audit_logs")
    op.drop_index(op.f("ix_secret_audit_logs_workspace_id"), table_name="secret_audit_logs")
    op.drop_table("secret_audit_logs")
