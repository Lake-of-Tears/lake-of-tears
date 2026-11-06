"""create deleted_catalog_assets table for 7-day soft-delete and UNDROP recovery

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deleted_catalog_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("catalog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("tier", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("s3_path_pattern", sa.Text(), nullable=True),
        sa.Column("column_defs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schema_drift", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("vector_index", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_managed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("trash_s3_prefix", sa.Text(), nullable=True),
        sa.Column("trash_embeddings_prefix", sa.Text(), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dropped_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["catalog_id"], ["catalogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dropped_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        op.f("ix_deleted_catalog_assets_workspace_id"),
        "deleted_catalog_assets",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deleted_catalog_assets_catalog_id"),
        "deleted_catalog_assets",
        ["catalog_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deleted_catalog_assets_batch_id"),
        "deleted_catalog_assets",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deleted_catalog_assets_purge_at"),
        "deleted_catalog_assets",
        ["purge_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_deleted_catalog_assets_purge_at"), table_name="deleted_catalog_assets")
    op.drop_index(op.f("ix_deleted_catalog_assets_batch_id"), table_name="deleted_catalog_assets")
    op.drop_index(op.f("ix_deleted_catalog_assets_catalog_id"), table_name="deleted_catalog_assets")
    op.drop_index(
        op.f("ix_deleted_catalog_assets_workspace_id"), table_name="deleted_catalog_assets"
    )
    op.drop_table("deleted_catalog_assets")
