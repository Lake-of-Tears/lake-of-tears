"""Iceberg table maintenance engine: compaction, snapshot expiration and orphan file cleanup.

* **Compaction (OPTIMIZE)** bin-packs partitions that accumulated many small data files into
  fewer large ones, as a single atomic overwrite snapshot.
* **Snapshot expiration** drops snapshots older than the retention window (always keeping the
  most recent ones and every branch/tag head) and deletes exactly the files only they used.
* **Orphan cleanup** removes files under the table's `data/` and `metadata/` directories that
  no retained snapshot or metadata version references (failed writes, superseded metadata).

Policies live per table in `iceberg_tables.maintenance["policy"]`; the nightly
`iceberg_table_maintenance` DAG and the OPTIMIZE / VACUUM API both run through here.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from iceberg_catalog import load_iceberg_table
from models import CatalogTable
from pyiceberg.expressions import AlwaysTrue, And, BooleanExpression, EqualTo, IsNull
from pyiceberg.manifest import DataFile, DataFileContent
from pyiceberg.table import Table
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.transforms import IdentityTransform
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("lake.backend.iceberg.maintenance")

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    # Compaction
    "compaction_enabled": True,
    "compaction_min_files": 5,
    "target_file_size_mb": 128,
    "max_rewrite_mb": 2048,
    # Snapshot expiration
    "snapshot_retention_days": 7,
    "retain_last_snapshots": 5,
    # Orphan cleanup: never touch files newer than this (in-flight writes)
    "orphan_older_than_hours": 72,
}
_TOON_FILES = ("table.toon", "table_files.toon", "version-hint.text")


def get_policy(row: CatalogTable, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    stored = ((row.iceberg.maintenance or {}) if row.iceberg else {}).get("policy") or {}
    policy = {**DEFAULT_POLICY, **{k: v for k, v in stored.items() if k in DEFAULT_POLICY}}
    for key, value in (overrides or {}).items():
        if key in DEFAULT_POLICY and value is not None:
            policy[key] = value
    return policy


def set_policy(db: Session, row: CatalogTable, updates: dict[str, Any]) -> dict[str, Any]:
    """Persist per-table policy overrides (validated against the known policy keys)."""
    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in DEFAULT_POLICY or value is None:
            continue
        default = DEFAULT_POLICY[key]
        if isinstance(default, bool):
            cleaned[key] = bool(value)
        else:
            number = int(value)
            if number < 0:
                raise ValueError(f"{key} must not be negative")
            cleaned[key] = number
    if cleaned.get("retain_last_snapshots") == 0:
        raise ValueError("retain_last_snapshots must be at least 1")
    state = dict(row.iceberg.maintenance or {})
    state["policy"] = {**(state.get("policy") or {}), **cleaned}
    row.iceberg.maintenance = state
    flag_modified(row.iceberg, "maintenance")
    db.commit()
    return get_policy(row)


# ── File inventory helpers ────────────────────────────────────────────────────


def _live_data_files(table: Table) -> list[DataFile]:
    snapshot = table.current_snapshot()
    if snapshot is None:
        return []
    files: list[DataFile] = []
    for manifest in snapshot.manifests(table.io):
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
            files.append(entry.data_file)
    return files


def _snapshot_files(
    table: Table, snapshot: Snapshot, manifest_cache: dict[str, set[str]]
) -> set[str]:
    """Every file a snapshot needs: its manifest list, manifests, data and delete files."""
    referenced = {snapshot.manifest_list} if snapshot.manifest_list else set()
    for manifest in snapshot.manifests(table.io):
        referenced.add(manifest.manifest_path)
        cached = manifest_cache.get(manifest.manifest_path)
        if cached is None:
            cached = {
                entry.data_file.file_path
                for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True)
            }
            manifest_cache[manifest.manifest_path] = cached
        referenced |= cached
    return referenced


def referenced_files(table: Table, snapshots: list[Snapshot] | None = None) -> set[str]:
    cache: dict[str, set[str]] = {}
    out: set[str] = set()
    for snapshot in snapshots if snapshots is not None else table.metadata.snapshots:
        out |= _snapshot_files(table, snapshot, cache)
    return out


def _metadata_files(table: Table) -> set[str]:
    keep = {table.metadata_location}
    keep |= {entry.metadata_file for entry in table.metadata.metadata_log}
    for stat in getattr(table.metadata, "statistics", None) or []:
        keep.add(stat.statistics_path)
    for stat in getattr(table.metadata, "partition_statistics", None) or []:
        keep.add(stat.statistics_path)
    base = table.metadata.location.rstrip("/")
    keep |= {f"{base}/metadata/{name}" for name in _TOON_FILES}
    return keep


def _list_files(prefix_uri: str) -> list[tuple[str, datetime]]:
    """List (uri, last_modified) for every object under a directory URI (s3:// or file://)."""
    scheme, _, rest = prefix_uri.partition("://")
    prefix_uri = prefix_uri.rstrip("/") + "/"
    out: list[tuple[str, datetime]] = []
    if scheme in ("s3", "s3a", "s3n"):
        from storage_service import get_s3_client

        bucket, _, key = rest.partition("/")
        key = key.rstrip("/") + "/"
        for page in (
            get_s3_client().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key)
        ):
            for item in page.get("Contents", []):
                if item["Key"].endswith("/"):
                    continue
                modified = item.get("LastModified") or datetime.now(UTC)
                out.append((f"{scheme}://{bucket}/{item['Key']}", modified))
    elif scheme == "file":
        root = prefix_uri[len("file://") :]
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                out.append(
                    (f"file://{path}", datetime.fromtimestamp(os.path.getmtime(path), tz=UTC))
                )
    return out


def _delete_files(table: Table, paths: set[str] | list[str]) -> int:
    deleted = 0
    for path in paths:
        try:
            table.io.delete(path)
            deleted += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Could not delete %s: %s", path, exc)
    return deleted


# ── Compaction ────────────────────────────────────────────────────────────────


def _partition_filter(table: Table, data_file: DataFile) -> BooleanExpression | None:
    """Row filter selecting exactly one identity partition, or None if not expressible."""
    spec = table.metadata.specs().get(data_file.spec_id)
    if spec is None:
        return None
    schema = table.metadata.schema()
    expr: BooleanExpression = AlwaysTrue()
    for pos, field in enumerate(spec.fields):
        if not isinstance(field.transform, IdentityTransform):
            return None
        column = schema.find_column_name(field.source_id)
        if column is None:
            return None
        value = data_file.partition[pos]
        term = IsNull(column) if value is None else EqualTo(column, value)
        expr = term if isinstance(expr, AlwaysTrue) else And(expr, term)
    return expr


def plan_compaction(table: Table, policy: dict[str, Any]) -> dict[str, Any]:
    """Group live data files by partition and pick the groups worth rewriting."""
    target_bytes = int(policy["target_file_size_mb"]) * 1024 * 1024
    min_files = max(2, int(policy["compaction_min_files"]))
    small_threshold = int(target_bytes * 0.75)

    files = _live_data_files(table)
    data_files = [f for f in files if f.content == DataFileContent.DATA]
    has_deletes = len(data_files) != len(files)
    current_spec = table.metadata.default_spec_id
    mixed_specs = any(f.spec_id != current_spec for f in data_files)

    groups: dict[tuple[Any, ...], list[DataFile]] = {}
    for f in data_files:
        key = (f.spec_id, *[f.partition[i] for i in range(len(f.partition))])
        groups.setdefault(key, []).append(f)

    candidates = []
    for key, members in groups.items():
        small = [f for f in members if f.file_size_in_bytes < small_threshold]
        if len(small) >= min_files:
            candidates.append((key, members))

    return {
        "data_files": len(data_files),
        "total_bytes": sum(f.file_size_in_bytes for f in data_files),
        "groups": len(groups),
        "candidates": candidates,
        # Delete files or partition evolution make per-partition rewrites unsafe to reason
        # about, so those tables are rewritten whole (the scan applies deletes correctly).
        "whole_table": bool(candidates)
        and (
            has_deletes
            or mixed_specs
            or not table.spec().fields
            or any(_partition_filter(table, members[0]) is None for _key, members in candidates)
        ),
    }


def compact_table(
    db: Session, row: CatalogTable, overrides: dict[str, Any] | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Bin-pack small files. Each run is one atomic snapshot; readers are never blocked."""
    policy = get_policy(row, overrides)
    table = load_iceberg_table(db, row)
    plan = plan_compaction(table, policy)
    result: dict[str, Any] = {
        "action": "compact",
        "files_before": plan["data_files"],
        "bytes_before": plan["total_bytes"],
        "partitions_total": plan["groups"],
        "partitions_compacted": 0,
        "files_rewritten": 0,
        "skipped": [],
        "dry_run": dry_run,
    }
    candidates = plan["candidates"]
    if not candidates:
        result.update(files_after=plan["data_files"], bytes_after=plan["total_bytes"])
        result["message"] = "Nothing to compact"
        return result

    max_bytes = int(policy["max_rewrite_mb"]) * 1024 * 1024
    props = {"lot.source": "maintenance:compaction"}

    if plan["whole_table"]:
        if plan["total_bytes"] > max_bytes:
            result["skipped"].append(
                f"table is {plan['total_bytes']} bytes, above max_rewrite_mb={policy['max_rewrite_mb']}"
            )
            result.update(files_after=plan["data_files"], bytes_after=plan["total_bytes"])
            return result
        result["partitions_compacted"] = plan["groups"]
        result["files_rewritten"] = plan["data_files"]
        if not dry_run:
            table.overwrite(table.scan().to_arrow(), snapshot_properties=props)
    else:
        budget = max_bytes
        chosen: list[tuple[BooleanExpression, int]] = []
        for _key, members in sorted(candidates, key=lambda c: -len(c[1])):
            size = sum(f.file_size_in_bytes for f in members)
            if size > budget:
                result["skipped"].append(
                    f"partition of {size} bytes exceeds remaining rewrite budget"
                )
                continue
            budget -= size
            chosen.append((_partition_filter(table, members[0]), len(members)))
        result["partitions_compacted"] = len(chosen)
        result["files_rewritten"] = sum(n for _f, n in chosen)
        if chosen and not dry_run:
            with table.transaction() as txn:
                for row_filter, _count in chosen:
                    data = table.scan(row_filter=row_filter).to_arrow()
                    txn.overwrite(data, overwrite_filter=row_filter, snapshot_properties=props)

    if dry_run or not result["files_rewritten"]:
        result.update(files_after=plan["data_files"], bytes_after=plan["total_bytes"])
        return result

    table = load_iceberg_table(db, row)
    after = [f for f in _live_data_files(table) if f.content == DataFileContent.DATA]
    snapshot = table.current_snapshot()
    result.update(
        files_after=len(after),
        bytes_after=sum(f.file_size_in_bytes for f in after),
        snapshot_id=str(snapshot.snapshot_id) if snapshot else None,
    )
    return result


# ── Snapshot expiration ───────────────────────────────────────────────────────


def expire_snapshots(
    db: Session, row: CatalogTable, overrides: dict[str, Any] | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Expire old snapshots and delete the files that only they referenced."""
    policy = get_policy(row, overrides)
    table = load_iceberg_table(db, row)
    metadata = table.metadata
    cutoff = datetime.now(UTC) - timedelta(days=int(policy["snapshot_retention_days"]))
    cutoff_ms = int(cutoff.timestamp() * 1000)
    retain_last = max(1, int(policy["retain_last_snapshots"]))

    protected = {ref.snapshot_id for ref in metadata.refs.values()}
    newest = sorted(metadata.snapshots, key=lambda s: s.timestamp_ms, reverse=True)[:retain_last]
    protected |= {s.snapshot_id for s in newest}
    to_expire = [
        s
        for s in metadata.snapshots
        if s.timestamp_ms < cutoff_ms and s.snapshot_id not in protected
    ]
    result: dict[str, Any] = {
        "action": "expire_snapshots",
        "snapshots_before": len(metadata.snapshots),
        "snapshots_expired": len(to_expire),
        "expired_snapshot_ids": [str(s.snapshot_id) for s in to_expire],
        "retention_days": int(policy["snapshot_retention_days"]),
        "files_deleted": 0,
        "dry_run": dry_run,
    }
    if not to_expire:
        return result

    expire_ids = {s.snapshot_id for s in to_expire}
    before = referenced_files(table)
    remaining = [s for s in metadata.snapshots if s.snapshot_id not in expire_ids]
    unreferenced = before - referenced_files(table, remaining)
    result["files_to_delete"] = len(unreferenced)
    if dry_run:
        return result

    table.maintenance.expire_snapshots().by_ids(sorted(expire_ids)).commit()
    # Files are removed only after the metadata commit succeeded, so a failure in between
    # leaves orphans (cleaned by the orphan sweep) rather than dangling references.
    table = load_iceberg_table(db, row)
    still_needed = referenced_files(table)
    result["files_deleted"] = _delete_files(table, unreferenced - still_needed)
    return result


# ── Orphan file cleanup ───────────────────────────────────────────────────────


def remove_orphan_files(
    db: Session, row: CatalogTable, overrides: dict[str, Any] | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Delete unreferenced files under the table's own `data/` and `metadata/` directories.

    The sweep is deliberately limited to those two directories: a zero-copy migrated table
    keeps its original Parquet files elsewhere under the location, and nothing outside the
    directories Iceberg itself writes to is ever considered an orphan.
    """
    policy = get_policy(row, overrides)
    table = load_iceberg_table(db, row)
    base = table.metadata.location.rstrip("/")
    cutoff = datetime.now(UTC) - timedelta(hours=int(policy["orphan_older_than_hours"]))

    keep = referenced_files(table) | _metadata_files(table)
    orphans: list[str] = []
    scanned = 0
    for directory in ("data", "metadata"):
        for uri, modified in _list_files(f"{base}/{directory}"):
            scanned += 1
            if uri in keep:
                continue
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            if modified > cutoff:
                continue
            orphans.append(uri)
    result: dict[str, Any] = {
        "action": "remove_orphan_files",
        "files_scanned": scanned,
        "orphan_files": len(orphans),
        "files_deleted": 0,
        "older_than_hours": int(policy["orphan_older_than_hours"]),
        "dry_run": dry_run,
    }
    if orphans and not dry_run:
        result["files_deleted"] = _delete_files(table, orphans)
    return result


# ── Orchestration ─────────────────────────────────────────────────────────────


def vacuum_table(
    db: Session, row: CatalogTable, overrides: dict[str, Any] | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """VACUUM = expire snapshots, then sweep orphan files."""
    expired = expire_snapshots(db, row, overrides, dry_run=dry_run)
    orphans = remove_orphan_files(db, row, overrides, dry_run=dry_run)
    return {
        "action": "vacuum",
        "dry_run": dry_run,
        "expire_snapshots": expired,
        "remove_orphan_files": orphans,
        "files_deleted": expired["files_deleted"] + orphans["files_deleted"],
    }


def run_table_maintenance(
    db: Session,
    row: CatalogTable,
    overrides: dict[str, Any] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Full maintenance pass for one table: compact → expire snapshots → remove orphans."""
    policy = get_policy(row, overrides)
    started = datetime.now(UTC)
    report: dict[str, Any] = {
        "table_id": str(row.id),
        "table": row.slug,
        "started_at": started.isoformat(),
        "dry_run": dry_run,
        "status": "ok",
        "steps": {},
    }
    if not policy["enabled"] and not force:
        report["status"] = "skipped"
        report["message"] = "Maintenance is disabled for this table"
        return report

    steps: list[tuple[str, Any]] = []
    if policy["compaction_enabled"]:
        steps.append(("compact", compact_table))
    steps += [("expire_snapshots", expire_snapshots), ("remove_orphan_files", remove_orphan_files)]
    for name, step in steps:
        try:
            report["steps"][name] = step(db, row, overrides, dry_run=dry_run)
        except Exception as exc:
            db.rollback()
            logger.exception("Maintenance step %s failed for table %s", name, row.slug)
            report["steps"][name] = {"action": name, "error": str(exc)}
            report["status"] = "error"

    report["finished_at"] = datetime.now(UTC).isoformat()
    if not dry_run:
        db.refresh(row)
        state = dict(row.iceberg.maintenance or {})
        state["last_run"] = report
        row.iceberg.maintenance = state
        flag_modified(row.iceberg, "maintenance")
        db.commit()
    return report


def list_iceberg_tables(db: Session, workspace_id: Any = None) -> list[CatalogTable]:
    from models import Catalog, CatalogSchema

    query = (
        db.query(CatalogTable)
        .join(CatalogSchema, CatalogSchema.id == CatalogTable.schema_id)
        .join(Catalog, Catalog.id == CatalogSchema.catalog_id)
        .filter(CatalogTable.format == "iceberg", Catalog.deleted_at.is_(None))
    )
    if workspace_id is not None:
        query = query.filter(Catalog.owner_workspace_id == workspace_id)
    return [t for t in query.all() if t.iceberg]
