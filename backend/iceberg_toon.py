"""Hierarchical 2-tier TOON metadata views for Iceberg tables.

* **Tier 1 — `table.toon`**: a compact table summary (schema, partitioning, current snapshot,
  row/file statistics, largest partitions) sized for an LLM context window.
* **Tier 2 — `table_files.toon`**: the file-level manifest (per-file partition, path, row
  count, size and min/max column bounds) for agents that prune partitions and files.

Both are regenerated synchronously by the catalog's commit hook, written next to the table's
Iceberg metadata (`<location>/metadata/table.toon`) and cached on the `iceberg_tables` row so
readers never touch object storage.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from models import CatalogTable
from pyiceberg.conversions import from_bytes
from pyiceberg.manifest import DataFile, DataFileContent
from pyiceberg.table import Table
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.types import PrimitiveType
from sqlalchemy.orm import Session
from toon import dump_toon

logger = logging.getLogger("lake.backend.iceberg.toon")

TIER1_FILE = "table.toon"
TIER2_FILE = "table_files.toon"

# Table properties that tune the generated views
PROP_MAX_FILES = "lot.toon.max-files"
PROP_BOUND_COLUMNS = "lot.toon.bound-columns"

DEFAULT_MAX_FILES = 2000
DEFAULT_BOUND_COLUMNS = 6
TIER1_MAX_PARTITIONS = 12
TIER1_MAX_DESCRIPTION = 160


def _clean(text: Any, limit: int | None = None) -> str:
    out = " ".join(str(text or "").split())
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary_int(snapshot: Snapshot | None, key: str) -> int:
    if snapshot is None or snapshot.summary is None:
        return 0
    try:
        return int(snapshot.summary.additional_properties.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _operation(snapshot: Snapshot | None) -> str | None:
    if snapshot is None or snapshot.summary is None:
        return None
    op = snapshot.summary.operation
    return getattr(op, "value", str(op))


def _partition_label(table: Table, data_file: DataFile) -> str:
    spec = table.metadata.specs().get(data_file.spec_id)
    if spec is None or not spec.fields:
        return ""
    try:
        return spec.partition_to_path(data_file.partition, table.metadata.schema())
    except Exception:
        return "/".join(f"{f.name}={data_file.partition[i]}" for i, f in enumerate(spec.fields))


def iter_data_files(table: Table, snapshot: Snapshot | None = None) -> Iterator[DataFile]:
    """Yield the live data files (no delete files) of a snapshot, manifest by manifest."""
    snapshot = snapshot or table.current_snapshot()
    if snapshot is None:
        return
    for manifest in snapshot.manifests(table.io):
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
            if entry.data_file.content == DataFileContent.DATA:
                yield entry.data_file


def _bound_columns(table: Table) -> list[tuple[int, str, PrimitiveType]]:
    schema = table.metadata.schema()
    wanted = [
        c.strip()
        for c in (table.metadata.properties.get(PROP_BOUND_COLUMNS) or "").split(",")
        if c.strip()
    ]
    fields = [f for f in schema.fields if isinstance(f.field_type, PrimitiveType)]
    if wanted:
        fields = [f for f in fields if f.name in wanted]
    else:
        fields = fields[:DEFAULT_BOUND_COLUMNS]
    return [(f.field_id, f.name, f.field_type) for f in fields]


def _bound(field_type: PrimitiveType, raw: bytes | None) -> Any:
    if raw is None:
        return None
    try:
        value = from_bytes(field_type, raw)
    except Exception:
        return None
    if isinstance(value, bytes):
        return None
    return _clean(value, 48) if isinstance(value, str) else value


def _relative(path: str, location: str) -> str:
    base = location.rstrip("/") + "/"
    return path[len(base) :] if path.startswith(base) else path


def _file_row(
    table: Table, data_file: DataFile, bounds: list[tuple[int, str, PrimitiveType]]
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "partition": _partition_label(table, data_file),
        "path": _relative(data_file.file_path, table.metadata.location),
        "rows": data_file.record_count,
        "bytes": data_file.file_size_in_bytes,
    }
    lower = data_file.lower_bounds or {}
    upper = data_file.upper_bounds or {}
    for field_id, name, field_type in bounds:
        row[f"{name}_min"] = _bound(field_type, lower.get(field_id))
        row[f"{name}_max"] = _bound(field_type, upper.get(field_id))
    return row


def _max_files(table: Table) -> int:
    try:
        return max(1, int(table.metadata.properties.get(PROP_MAX_FILES) or DEFAULT_MAX_FILES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILES


def build_table_toon(
    table: Table,
    row: CatalogTable | None = None,
    *,
    catalog_slug: str = "",
    schema_slug: str = "",
    file_rows: list[dict[str, Any]] | None = None,
    files_truncated: bool = False,
) -> str:
    """Tier 1: compact table summary for LLM context windows."""
    metadata = table.metadata
    snapshot = table.current_snapshot()
    schema = metadata.schema()
    spec = metadata.spec()
    described = {
        str(c.get("name")): c.get("description")
        for c in ((row.column_defs if row is not None else None) or [])
        if isinstance(c, dict)
    }

    from iceberg_catalog import iceberg_type_to_sql

    data: dict[str, Any] = {"table": row.slug if row is not None else table.name()[-1]}
    if catalog_slug:
        data["catalog"] = catalog_slug
    if schema_slug:
        data["schema"] = schema_slug
    data["format"] = f"iceberg-v{metadata.format_version}"
    description = _clean(row.description if row is not None else "", 240)
    if description:
        data["description"] = description
    data["location"] = metadata.location

    if snapshot is not None:
        snap: dict[str, Any] = {
            "id": snapshot.snapshot_id,
            "operation": _operation(snapshot),
            "committed_at": _iso(snapshot.timestamp_ms),
        }
        if snapshot.parent_snapshot_id is not None:
            snap["parent_id"] = snapshot.parent_snapshot_id
        data["snapshot"] = snap

    partition_stats: dict[str, dict[str, int]] = {}
    for f in file_rows or []:
        agg = partition_stats.setdefault(f["partition"], {"rows": 0, "files": 0, "bytes": 0})
        agg["rows"] += int(f["rows"] or 0)
        agg["files"] += 1
        agg["bytes"] += int(f["bytes"] or 0)

    stats: dict[str, Any] = {
        "rows": _summary_int(snapshot, "total-records"),
        "data_files": _summary_int(snapshot, "total-data-files"),
        "size_bytes": _summary_int(snapshot, "total-files-size"),
        "snapshots": len(metadata.snapshots),
    }
    deletes = _summary_int(snapshot, "total-delete-files")
    if deletes:
        stats["delete_files"] = deletes
    if spec.fields and not files_truncated:
        stats["partitions"] = len(partition_stats)
    data["stats"] = stats

    if spec.fields:
        data["partitioned_by"] = [
            f"{schema.find_column_name(f.source_id) or f.name}"
            if str(f.transform) == "identity"
            else f"{f.transform}({schema.find_column_name(f.source_id) or f.name})"
            for f in spec.fields
        ]

    data["columns"] = [
        {
            "name": f.name,
            "type": iceberg_type_to_sql(f.field_type),
            "nullable": not f.required,
            "description": _clean(described.get(f.name) or f.doc, TIER1_MAX_DESCRIPTION),
        }
        for f in schema.fields
    ]

    if spec.fields and partition_stats:
        ordered = sorted(partition_stats.items(), key=lambda kv: kv[0], reverse=True)
        data["partitions"] = [
            {"partition": name, **agg} for name, agg in ordered[:TIER1_MAX_PARTITIONS]
        ]
        if len(ordered) > TIER1_MAX_PARTITIONS or files_truncated:
            data["partitions_truncated"] = True

    if snapshot is not None:
        data["time_travel"] = "append FOR SYSTEM_VERSION AS OF <snapshot_id> to the table name"
    return dump_toon(data)


def build_files_toon(
    table: Table,
    row: CatalogTable | None = None,
    *,
    catalog_slug: str = "",
    schema_slug: str = "",
    file_rows: list[dict[str, Any]],
    truncated: bool = False,
    partition: str | None = None,
) -> str:
    """Tier 2: file-level manifest with per-file bounds for partition and file pruning."""
    snapshot = table.current_snapshot()
    data: dict[str, Any] = {"table": row.slug if row is not None else table.name()[-1]}
    if catalog_slug:
        data["catalog"] = catalog_slug
    if schema_slug:
        data["schema"] = schema_slug
    data["snapshot_id"] = snapshot.snapshot_id if snapshot is not None else None
    data["location"] = table.metadata.location
    if partition:
        data["partition_filter"] = partition
    data["file_count"] = len(file_rows)
    data["total_data_files"] = _summary_int(snapshot, "total-data-files")
    if truncated:
        data["truncated"] = True
    data["paths"] = "relative to location unless absolute"
    if file_rows:
        data["files"] = file_rows
    return dump_toon(data)


def collect_file_rows(
    table: Table,
    *,
    partition: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Read file rows for Tier 2, optionally sliced by a partition substring. Returns (rows, truncated)."""
    limit = limit or _max_files(table)
    bounds = _bound_columns(table)
    needle = (partition or "").strip().lower()
    rows: list[dict[str, Any]] = []
    skipped = 0
    truncated = False
    for data_file in iter_data_files(table):
        if needle and needle not in _partition_label(table, data_file).lower():
            continue
        if skipped < offset:
            skipped += 1
            continue
        if len(rows) >= limit:
            truncated = True
            break
        rows.append(_file_row(table, data_file, bounds))
    return rows, truncated


def generate(
    table: Table, row: CatalogTable | None = None, catalog_slug: str = "", schema_slug: str = ""
) -> tuple[str, str]:
    """Build (tier1, tier2) TOON text for the table's current snapshot."""
    file_rows, truncated = collect_file_rows(table)
    tier1 = build_table_toon(
        table,
        row,
        catalog_slug=catalog_slug,
        schema_slug=schema_slug,
        file_rows=file_rows,
        files_truncated=truncated,
    )
    tier2 = build_files_toon(
        table,
        row,
        catalog_slug=catalog_slug,
        schema_slug=schema_slug,
        file_rows=file_rows,
        truncated=truncated,
    )
    return tier1, tier2


def _write_text(table: Table, name: str, text: str) -> str:
    path = f"{table.metadata.location.rstrip('/')}/metadata/{name}"
    with table.io.new_output(path).create(overwrite=True) as out:
        out.write(text.encode("utf-8"))
    return path


def refresh_table_toon(db: Session, row: CatalogTable, catalog: Any = None) -> tuple[str, str]:
    """Regenerate both TOON tiers for a table, persist them to storage and cache them in PostgreSQL."""
    if catalog is None:
        from iceberg_catalog import owner_workspace_of, system_catalog

        catalog = system_catalog(db, owner_workspace_of(row))
    table = catalog.load_table_row(row)
    schema_row = row.schema
    tier1, tier2 = generate(
        table,
        row,
        catalog_slug=schema_row.catalog.slug if schema_row and schema_row.catalog else "",
        schema_slug=schema_row.slug if schema_row else "",
    )
    try:
        _write_text(table, TIER1_FILE, tier1)
        _write_text(table, TIER2_FILE, tier2)
    except Exception as exc:
        logger.warning("Could not persist TOON views for %s to storage: %s", row.slug, exc)
    row.iceberg.toon_summary = tier1
    row.iceberg.toon_files = tier2
    row.iceberg.toon_generated_at = datetime.now(UTC)
    db.commit()
    return tier1, tier2


def get_table_toon(db: Session, row: CatalogTable, tier: int = 1) -> str:
    """Return the cached TOON view for a tier, generating it on a cold cache."""
    ice = row.iceberg
    cached = ice.toon_summary if tier == 1 else ice.toon_files
    if cached:
        return cached
    tier1, tier2 = refresh_table_toon(db, row)
    return tier1 if tier == 1 else tier2


def slice_files_toon(
    db: Session,
    row: CatalogTable,
    *,
    partition: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> str:
    """Parameterized Tier 2 slice read live from the manifests (partition filter / paging)."""
    from iceberg_catalog import load_iceberg_table

    table = load_iceberg_table(db, row)
    file_rows, truncated = collect_file_rows(table, partition=partition, limit=limit, offset=offset)
    schema_row = row.schema
    return build_files_toon(
        table,
        row,
        catalog_slug=schema_row.catalog.slug if schema_row and schema_row.catalog else "",
        schema_slug=schema_row.slug if schema_row else "",
        file_rows=file_rows,
        truncated=truncated,
        partition=partition,
    )
