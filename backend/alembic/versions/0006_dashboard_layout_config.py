"""add layout_config to workspace_dashboards

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace_dashboards",
        sa.Column(
            "layout_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.alter_column(
        "workspace_dashboards",
        "superset_dashboard_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "workspace_dashboards",
        "embedded_uuid",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "workspace_dashboards",
        "embedded_uuid",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "workspace_dashboards",
        "superset_dashboard_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_column("workspace_dashboards", "layout_config")
