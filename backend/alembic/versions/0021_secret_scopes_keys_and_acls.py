"""create secret_scopes, secret_keys, and secret_acls tables

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("backend_type", sa.String(length=64), nullable=False, server_default="internal"),
        sa.Column("provider_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_workspace_scope_name"),
    )
    op.create_index(
        op.f("ix_secret_scopes_workspace_id"),
        "secret_scopes",
        ["workspace_id"],
        unique=False,
    )

    op.create_table(
        "secret_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scope_id"], ["secret_scopes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("scope_id", "key", name="uq_scope_secret_key"),
    )
    op.create_index(
        op.f("ix_secret_keys_scope_id"),
        "secret_keys",
        ["scope_id"],
        unique=False,
    )

    op.create_table(
        "secret_acls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("permission", sa.String(length=32), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scope_id"], ["secret_scopes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "scope_id", "principal_type", "principal_id", name="uq_scope_acl_principal"
        ),
    )
    op.create_index(
        op.f("ix_secret_acls_scope_id"),
        "secret_acls",
        ["scope_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_secret_acls_scope_id"), table_name="secret_acls")
    op.drop_table("secret_acls")
    op.drop_index(op.f("ix_secret_keys_scope_id"), table_name="secret_keys")
    op.drop_table("secret_keys")
    op.drop_index(op.f("ix_secret_scopes_workspace_id"), table_name="secret_scopes")
    op.drop_table("secret_scopes")
