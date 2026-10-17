"""add vector_index to catalog_tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "catalog_tables",
        sa.Column(
            "vector_index",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("catalog_tables", "vector_index")
