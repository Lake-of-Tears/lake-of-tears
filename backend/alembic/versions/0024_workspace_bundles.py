"""create workspace_bundles table for Lake Asset Bundles state tracking

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_bundles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bundle_name", sa.String(length=255), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False, server_default=sa.text("'dev'")),
        sa.Column(
            "version", sa.String(length=64), nullable=False, server_default=sa.text("'0.1.0'")
        ),
        sa.Column("git_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("deployed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default=sa.text("'deployed'")
        ),
        sa.Column(
            "manifest_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "resources_map",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_deployed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["deployed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id", "bundle_name", "target", name="uq_workspace_bundle_target"
        ),
    )
    op.create_index(
        op.f("ix_workspace_bundles_workspace_id"),
        "workspace_bundles",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_bundles_bundle_name"),
        "workspace_bundles",
        ["bundle_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_bundles_target"),
        "workspace_bundles",
        ["target"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_bundles_status"),
        "workspace_bundles",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_bundles_deployed_by"),
        "workspace_bundles",
        ["deployed_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workspace_bundles_deployed_by"),
        table_name="workspace_bundles",
    )
    op.drop_index(
        op.f("ix_workspace_bundles_status"),
        table_name="workspace_bundles",
    )
    op.drop_index(
        op.f("ix_workspace_bundles_target"),
        table_name="workspace_bundles",
    )
    op.drop_index(
        op.f("ix_workspace_bundles_bundle_name"),
        table_name="workspace_bundles",
    )
    op.drop_index(
        op.f("ix_workspace_bundles_workspace_id"),
        table_name="workspace_bundles",
    )
    op.drop_table("workspace_bundles")
