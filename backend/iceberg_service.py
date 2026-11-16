"""Snapshot history, time-travel helpers and RESTORE for Iceberg catalog tables."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from iceberg_catalog import load_iceberg_table
from models import Catalog, CatalogSchema, CatalogTable, User, Workspace
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.iceberg.service")


def _iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()


def _int(props: dict[str, str], key: str) -> int:
    try:
        return int(props.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def list_snapshots(db: Session, row: CatalogTable) -> dict[str, Any]:
    """Snapshot history (newest first) with operation types and record deltas."""
    table = load_iceberg_table(db, row)
    metadata = table.metadata
    current_id = metadata.current_snapshot_id
    snapshots: list[dict[str, Any]] = []
    for snap in sorted(metadata.snapshots, key=lambda s: s.timestamp_ms, reverse=True):
        props = dict(snap.summary.additional_properties) if snap.summary else {}
        operation = getattr(snap.summary.operation, "value", None) if snap.summary else None
        added, deleted = _int(props, "added-records"), _int(props, "deleted-records")
        snapshots.append(
            {
                "snapshot_id": str(snap.snapshot_id),
                "parent_id": str(snap.parent_snapshot_id) if snap.parent_snapshot_id else None,
                "committed_at": _iso(snap.timestamp_ms),
                "operation": operation,
                "is_current": snap.snapshot_id == current_id,
                "added_records": added,
                "deleted_records": deleted,
                "record_delta": added - deleted,
                "total_records": _int(props, "total-records"),
                "added_data_files": _int(props, "added-data-files"),
                "deleted_data_files": _int(props, "deleted-data-files"),
                "total_data_files": _int(props, "total-data-files"),
                "total_size_bytes": _int(props, "total-files-size"),
                "schema_id": snap.schema_id,
                "source": props.get("lot.source"),
            }
        )
    return {
        "table_id": str(row.id),
        "table": row.slug,
        "format_version": metadata.format_version,
        "location": metadata.location,
        "metadata_location": table.metadata_location,
        # Snapshot ids exceed JavaScript's safe integer range, so they travel as strings.
        "current_snapshot_id": str(current_id) if current_id is not None else None,
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
        "refs": {name: str(ref.snapshot_id) for name, ref in metadata.refs.items()},
    }


def restore_table(
    db: Session,
    row: CatalogTable,
    *,
    snapshot_id: int | str | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Roll the table's `main` branch back (or forward) to a retained snapshot.

    Nothing is deleted: the restore is itself a metadata commit, so it can be undone by
    restoring the previously current snapshot.
    """
    table = load_iceberg_table(db, row)
    metadata = table.metadata
    previous_id = metadata.current_snapshot_id

    target = None
    if snapshot_id is not None:
        try:
            target = metadata.snapshot_by_id(int(snapshot_id))
        except (TypeError, ValueError):
            target = None
        if target is None:
            raise ValueError(f"Snapshot {snapshot_id} does not exist (it may have been expired)")
    elif as_of is not None:
        ts = as_of if isinstance(as_of, datetime) else datetime.fromisoformat(str(as_of).strip())
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        cutoff_ms = int(ts.timestamp() * 1000)
        candidates = [s for s in metadata.snapshots if s.timestamp_ms <= cutoff_ms]
        if not candidates:
            raise ValueError(f"No snapshot exists at or before {ts.isoformat()}")
        target = max(candidates, key=lambda s: s.timestamp_ms)
    else:
        raise ValueError("Provide a snapshot_id or an as_of timestamp to restore to")

    if target.snapshot_id == previous_id:
        return {
            "restored": False,
            "message": "Table is already at this snapshot",
            "snapshot_id": str(previous_id),
            "previous_snapshot_id": str(previous_id),
        }

    table.manage_snapshots().set_current_snapshot(snapshot_id=target.snapshot_id).commit()
    db.refresh(row)
    return {
        "restored": True,
        "snapshot_id": str(target.snapshot_id),
        "previous_snapshot_id": str(previous_id) if previous_id is not None else None,
        "committed_at": _iso(target.timestamp_ms),
        "message": f"Restored {row.slug} to snapshot {target.snapshot_id}",
    }


_RESTORE_RE = re.compile(
    r"""^RESTORE\s+(?:TABLE\s+)?(?P<target>[^\s;]+)\s+TO\s+
        (?:
            (?:VERSION\s+AS\s+OF|SNAPSHOT)\s+(?P<version>\d+)
            |
            TIMESTAMP\s+AS\s+OF\s+(?:TIMESTAMP\s+)?'(?P<ts>[^']+)'
        )\s*;?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _safe(name: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip()).lower()


def try_handle_restore_statement(
    sql: str,
    workspace: Workspace,
    user: User,
    default_schema: str | None,
    db: Session,
) -> tuple[bool, str | None]:
    """Intercept `RESTORE TABLE [catalog.][schema.]table TO VERSION AS OF <id>` (or
    `TO TIMESTAMP AS OF '<ts>'`). Returns (is_restore, message_or_error)."""
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned.upper().startswith("RESTORE"):
        return False, None
    match = _RESTORE_RE.match(cleaned)
    if not match:
        return (
            True,
            "Error: Usage: RESTORE TABLE [catalog.][schema.]table TO VERSION AS OF <snapshot_id> "
            "| TO TIMESTAMP AS OF '<timestamp>'",
        )

    parts = [p.strip().strip('"') for p in match.group("target").split(".")]
    table_name = _safe(parts[-1])
    schema_name = _safe(parts[-2]) if len(parts) >= 2 else None
    catalog_name = _safe(parts[-3]) if len(parts) >= 3 else None

    # Restores rewrite table history, so only tables in catalogs this workspace owns qualify.
    query = (
        db.query(CatalogTable)
        .join(CatalogSchema, CatalogSchema.id == CatalogTable.schema_id)
        .join(Catalog, Catalog.id == CatalogSchema.catalog_id)
        .filter(Catalog.owner_workspace_id == workspace.id, Catalog.deleted_at.is_(None))
    )
    matches = [
        t
        for t in query.all()
        if _safe(t.slug) == table_name
        and (schema_name is None or _safe(t.schema.slug) == schema_name)
        and (
            catalog_name is None
            or catalog_name in (_safe(t.schema.catalog.slug), _safe(t.schema.catalog.name))
        )
    ]
    if schema_name is None and default_schema and len(matches) > 1:
        narrowed = [t for t in matches if _safe(t.schema.slug) == _safe(default_schema)]
        matches = narrowed or matches
    if not matches:
        return True, f"Error: Table '{match.group('target')}' was not found in this workspace."
    if len(matches) > 1:
        return True, f"Error: Table name '{match.group('target')}' is ambiguous; qualify it."
    row = matches[0]
    if row.format != "iceberg" or not row.iceberg:
        return (
            True,
            f"Error: RESTORE is only supported for Iceberg tables; '{row.slug}' is Parquet.",
        )
    try:
        result = restore_table(db, row, snapshot_id=match.group("version"), as_of=match.group("ts"))
    except Exception as exc:
        return True, f"Error: RESTORE failed: {exc}"
    logger.info("User %s restored table %s: %s", getattr(user, "email", "?"), row.slug, result)
    return True, result["message"]
