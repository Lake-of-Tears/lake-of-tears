"""add pipeline_metadata table

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dag_id", sa.String(255), nullable=False),
        sa.Column("color_hex", sa.String(7), nullable=False),
        sa.Column("display_label", sa.String(255), nullable=True),
        sa.Column("is_visible_on_timeline", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "dag_id", name="uq_pipeline_metadata_ws_dag"),
    )
    op.create_index(op.f("ix_pipeline_metadata_workspace_id"), "pipeline_metadata", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_pipeline_metadata_dag_id"), "pipeline_metadata", ["dag_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pipeline_metadata_dag_id"), table_name="pipeline_metadata")
    op.drop_index(op.f("ix_pipeline_metadata_workspace_id"), table_name="pipeline_metadata")
    op.drop_table("pipeline_metadata")
