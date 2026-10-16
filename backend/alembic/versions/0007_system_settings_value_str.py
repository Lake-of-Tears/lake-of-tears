"""add value_str to system_settings

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "system_settings",
        sa.Column("value_str", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("system_settings", "value_str")
