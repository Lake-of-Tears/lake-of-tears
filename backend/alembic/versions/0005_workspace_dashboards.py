"""add workspace_dashboards table

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_dashboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("superset_dashboard_id", sa.Integer(), nullable=False),
        sa.Column("embedded_uuid", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("charts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_workspace_dashboard_slug"),
    )
    op.create_index(
        op.f("ix_workspace_dashboards_workspace_id"),
        "workspace_dashboards",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_dashboards_superset_dashboard_id"),
        "workspace_dashboards",
        ["superset_dashboard_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workspace_dashboards_superset_dashboard_id"),
        table_name="workspace_dashboards",
    )
    op.drop_index(
        op.f("ix_workspace_dashboards_workspace_id"),
        table_name="workspace_dashboards",
    )
    op.drop_table("workspace_dashboards")
