"""In-process PyIceberg ACID writes for backend flows (SQL editor DML, create-table, uploads).

Pipelines and external engines write through the Iceberg REST catalog instead; both paths end
in `LotIcebergCatalog.commit_identifier`, so every write gets the same optimistic-concurrency
check and the same on-commit TOON regeneration.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
from iceberg_catalog import load_iceberg_table
from models import CatalogTable
from pyiceberg.table import Table
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.iceberg.writer")

MODE_APPEND = "append"
MODE_OVERWRITE = "overwrite"


def normalize_arrow(data: pa.Table) -> pa.Table:
    """Coerce Arrow types DuckDB / pandas emit that Iceberg has no equivalent for."""
    fields: list[pa.Field] = []
    changed = False
    for field in data.schema:
        t = field.type
        new_type = t
        if (
            pa.types.is_uint8(t)
            or pa.types.is_uint16(t)
            or pa.types.is_int8(t)
            or pa.types.is_int16(t)
        ):
            new_type = pa.int32()
        elif pa.types.is_uint32(t):
            new_type = pa.int64()
        elif pa.types.is_uint64(t):
            new_type = pa.decimal128(20, 0)
        elif pa.types.is_null(t):
            new_type = pa.string()
        elif pa.types.is_timestamp(t) and t.unit != "us":
            new_type = pa.timestamp("us", tz=t.tz)
        elif pa.types.is_time(t) and getattr(t, "unit", "us") != "us":
            new_type = pa.time64("us")
        elif pa.types.is_float16(t):
            new_type = pa.float32()
        if new_type is not t:
            changed = True
        fields.append(pa.field(field.name, new_type, nullable=True))
    if not changed and all(f.nullable for f in data.schema):
        return data
    return data.cast(pa.schema(fields), safe=False)


def _evolve_schema(table: Table, data: pa.Table, allow_column_drop: bool) -> None:
    existing = set(table.schema().column_names)
    incoming = set(data.schema.names)
    partition_sources = {table.schema().find_column_name(f.source_id) for f in table.spec().fields}
    to_drop = (existing - incoming - partition_sources) if allow_column_drop else set()
    if incoming <= existing and not to_drop:
        try:
            # Still let PyIceberg widen types (int -> long, float -> double) when needed.
            with table.update_schema() as update:
                update.union_by_name(data.schema)
        except Exception as exc:
            logger.debug("No schema promotion applied: %s", exc)
        return
    with table.update_schema(allow_incompatible_changes=bool(to_drop)) as update:
        update.union_by_name(data.schema)
        for name in sorted(to_drop):
            update.delete_column(name)


def write_arrow(
    db: Session,
    row: CatalogTable,
    data: pa.Table,
    *,
    mode: str = MODE_APPEND,
    evolve_schema: bool = True,
    allow_column_drop: bool = False,
    snapshot_properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Atomically append to, or overwrite, an Iceberg catalog table. Returns the new snapshot info."""
    if mode not in (MODE_APPEND, MODE_OVERWRITE):
        raise ValueError(f"Unsupported write mode '{mode}' (use 'append' or 'overwrite')")
    table = load_iceberg_table(db, row)
    data = normalize_arrow(data)
    if evolve_schema:
        _evolve_schema(table, data, allow_column_drop and mode == MODE_OVERWRITE)
    props = dict(snapshot_properties or {})
    if mode == MODE_OVERWRITE:
        table.overwrite(data, snapshot_properties=props)
    else:
        table.append(data, snapshot_properties=props)
    snapshot = table.current_snapshot()
    db.refresh(row)
    return {
        "mode": mode,
        "rows_written": data.num_rows,
        "snapshot_id": snapshot.snapshot_id if snapshot else None,
        "metadata_location": table.metadata_location,
    }
