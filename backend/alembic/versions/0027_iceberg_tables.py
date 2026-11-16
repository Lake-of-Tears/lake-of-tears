"""add catalog_tables.format and iceberg_tables pointer table for the native Iceberg REST catalog

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "catalog_tables",
        sa.Column("format", sa.String(length=20), nullable=False, server_default="parquet"),
    )
    op.add_column(
        "deleted_catalog_assets",
        sa.Column("format", sa.String(length=20), nullable=False, server_default="parquet"),
    )
    op.add_column(
        "deleted_catalog_assets",
        sa.Column("iceberg_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "iceberg_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_uuid", sa.String(length=36), nullable=True),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("metadata_location", sa.Text(), nullable=False),
        sa.Column("previous_metadata_location", sa.Text(), nullable=True),
        sa.Column("current_schema_id", sa.Integer(), nullable=True),
        sa.Column("current_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("toon_summary", sa.Text(), nullable=True),
        sa.Column("toon_files", sa.Text(), nullable=True),
        sa.Column("toon_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("maintenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["table_id"], ["catalog_tables.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_iceberg_tables_table_id"), "iceberg_tables", ["table_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_iceberg_tables_table_id"), table_name="iceberg_tables")
    op.drop_table("iceberg_tables")
    op.drop_column("deleted_catalog_assets", "iceberg_state")
    op.drop_column("deleted_catalog_assets", "format")
    op.drop_column("catalog_tables", "format")
