"""scope catalog_access unique constraint to catalog_id, workspace_id, requested_by

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_catalog_access", "catalog_access", type_="unique")
    op.create_unique_constraint(
        "uq_catalog_access_user",
        "catalog_access",
        ["catalog_id", "workspace_id", "requested_by"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_catalog_access_user", "catalog_access", type_="unique")
    op.create_unique_constraint(
        "uq_catalog_access",
        "catalog_access",
        ["catalog_id", "workspace_id"],
    )
