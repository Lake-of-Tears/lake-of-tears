"""In-place, zero-copy migration of Parquet catalog tables to Apache Iceberg.

The engine never rewrites or moves a data file. It reads each existing Parquet footer
(schema, row count, column metrics), builds Iceberg metadata that references those files
where they already are, and then flips the catalog row to `format = 'iceberg'` in a single
transaction. Hive-style `key=value` directories become identity partitions whose values are
recorded in the manifests, so partition columns keep resolving exactly as before.

A migration is staged against a throwaway in-memory catalog and only adopted by the LoT
catalog once the Iceberg view of the table has been verified against the Parquet view, so a
failed or aborted run leaves the table untouched.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from iceberg_catalog import (
    LotIcebergCatalog,
    detect_iceberg_metadata,
    is_within_workspace,
    owner_workspace_of,
    schema_from_column_defs,
    storage_properties,
    system_catalog,
    warehouse_root,
)
from iceberg_maintenance import _delete_files, _list_files
from models import Catalog, CatalogTable
from pyiceberg.io.pyarrow import (
    compute_statistics_plan,
    data_file_statistics_from_parquet_metadata,
    parquet_path_to_id_mapping,
)
from pyiceberg.manifest import DataFile, DataFileContent, FileFormat
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import TableProperties
from pyiceberg.typedef import Record
from pyiceberg.types import DateType, IcebergType, LongType, NestedField, StringType
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from table_scan import ensure_iceberg_loaded, table_read_scan

logger = logging.getLogger("lake.backend.iceberg.migration")

_HIVE_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_INT_RE = re.compile(r"^(?:0|-?[1-9]\d*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HIVE_NULL = "__HIVE_DEFAULT_PARTITION__"


class MigrationSkipped(Exception):
    """The table cannot (or need not) be migrated; carries the user-facing reason."""


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Translate a LoT glob (`**`, `*`, `?`) into a regex over full object URIs."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _table_root(pattern: str) -> str:
    """Directory that will become the Iceberg table location for a Parquet path pattern."""
    glob = re.search(r"[*?\[]", pattern)
    head = pattern[: glob.start()] if glob else pattern
    if glob or head.lower().endswith(".parquet"):
        head = head.rsplit("/", 1)[0]
    root = head.rstrip("/")
    # Stop above any hive partition directories that precede the glob
    parts = root.split("/")
    while parts and _HIVE_SEGMENT.match(parts[-1]):
        parts.pop()
    return "/".join(parts)


def _hive_partition(relative_path: str) -> list[tuple[str, str]]:
    return [
        (m.group(1), m.group(2))
        for m in (_HIVE_SEGMENT.match(seg) for seg in relative_path.split("/")[:-1])
        if m
    ]


def _infer_partition_type(values: list[str]) -> IcebergType:
    present = [v for v in values if v != _HIVE_NULL]
    if present and all(_INT_RE.match(v) for v in present):
        return LongType()
    if present and all(_DATE_RE.match(v) for v in present):
        return DateType()
    return StringType()


def _partition_value(raw: str, field_type: IcebergType) -> Any:
    if raw == _HIVE_NULL:
        return None
    if isinstance(field_type, LongType):
        return int(raw)
    if isinstance(field_type, DateType):
        return (date.fromisoformat(raw) - date(1970, 1, 1)).days
    return raw


def _overlapping_tables(db: Session, row: CatalogTable, root: str) -> list[str]:
    """Other catalog tables whose storage overlaps this table's would-be Iceberg location."""
    prefix = root.rstrip("/") + "/"
    clashes = []
    for other in db.query(CatalogTable).filter(CatalogTable.id != row.id).all():
        pattern = (other.s3_path_pattern or "").strip()
        if not pattern:
            continue
        other_root = (
            pattern.rstrip("/") if other.format == "iceberg" else _table_root(pattern)
        ).rstrip("/") + "/"
        if other_root.startswith(prefix) or prefix.startswith(other_root):
            clashes.append(other.slug)
    return clashes


def plan_table_migration(db: Session, row: CatalogTable) -> dict[str, Any]:
    """Inspect a table and describe what migrating it would do. Raises MigrationSkipped."""
    if row.format == "iceberg":
        raise MigrationSkipped("already an Iceberg table")
    pattern = (row.s3_path_pattern or "").strip()
    if not pattern:
        raise MigrationSkipped("table has no storage path")
    owner = owner_workspace_of(row)
    root = _table_root(pattern)
    if not is_within_workspace(root + "/", owner.slug):
        raise MigrationSkipped(
            "external table: its data lives outside the workspace's managed storage, "
            "so Iceberg metadata cannot be written next to it"
        )
    clashes = _overlapping_tables(db, row, root)
    if clashes:
        raise MigrationSkipped(
            f"storage prefix is shared with other tables ({', '.join(sorted(clashes)[:5])})"
        )
    existing_meta = detect_iceberg_metadata(root)
    if existing_meta:
        return {
            "table_id": str(row.id),
            "table": row.slug,
            "pattern": pattern,
            "location": root,
            "files": 0,
            "rows": 0,
            "bytes": 0,
            "columns": [],
            "partitioned_by": [],
            "_existing_metadata": existing_meta,
            "_footers": [],
            "_hive": [],
            "_arrow_schema": None,
            "_iceberg_schema": None,
            "_partition_types": {},
        }

    matcher = _pattern_regex(pattern)
    files = sorted(
        uri
        for uri, _modified in _list_files(root)
        if matcher.match(uri) and "/metadata/" not in uri[len(root) :]
    )
    if not files:
        if row.column_defs:
            try:
                ice_schema = schema_from_column_defs(row.column_defs)
            except Exception:
                ice_schema = Schema(NestedField(1, "id", LongType(), required=False))
        else:
            ice_schema = Schema(NestedField(1, "id", LongType(), required=False))
        return {
            "table_id": str(row.id),
            "table": row.slug,
            "pattern": pattern,
            "location": root,
            "files": 0,
            "rows": 0,
            "bytes": 0,
            "columns": [f.name for f in ice_schema.fields],
            "partitioned_by": [],
            "_footers": [],
            "_hive": [],
            "_arrow_schema": None,
            "_iceberg_schema": ice_schema,
            "_partition_types": {},
        }

    io = system_catalog(db, owner)._io({}, root)
    footers: list[tuple[str, int, Any]] = []
    for uri in files:
        input_file = io.new_input(uri)
        with input_file.open() as stream:
            footers.append((uri, len(input_file), pq.read_metadata(stream)))

    try:
        arrow_schema = pa.unify_schemas(
            [meta.schema.to_arrow_schema() for _uri, _size, meta in footers],
            promote_options="permissive",
        )
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise MigrationSkipped(f"Parquet files have conflicting schemas: {exc}") from exc

    if any(pa.types.is_null(f.type) for f in arrow_schema):
        new_fields = []
        for f in arrow_schema:
            if pa.types.is_null(f.type):
                ptype = None
                for _uri, _sz, meta in footers:
                    try:
                        col_idx = meta.schema.names.index(f.name)
                        if meta.num_row_groups > 0:
                            ptype = meta.row_group(0).column(col_idx).physical_type
                            break
                    except (ValueError, IndexError):
                        continue
                if ptype in ("INT32", "INT64"):
                    new_fields.append(
                        pa.field(
                            f.name, pa.int32() if ptype == "INT32" else pa.int64(), nullable=True
                        )
                    )
                elif ptype in ("FLOAT", "DOUBLE"):
                    new_fields.append(pa.field(f.name, pa.float64(), nullable=True))
                elif ptype in ("BOOLEAN",):
                    new_fields.append(pa.field(f.name, pa.bool_(), nullable=True))
                else:
                    new_fields.append(pa.field(f.name, pa.string(), nullable=True))
            else:
                new_fields.append(f)
        arrow_schema = pa.schema(new_fields)

    file_columns = set(arrow_schema.names)
    hive = [_hive_partition(uri[len(root) + 1 :]) for uri in files]
    keys = [k for k, _v in hive[0] if k not in file_columns]
    for parts in hive:
        if [k for k, _v in parts if k not in file_columns] != keys:
            raise MigrationSkipped("files use inconsistent hive partition directories")
    partition_types = {
        key: _infer_partition_type([dict(parts)[key] for parts in hive]) for key in keys
    }

    return {
        "table_id": str(row.id),
        "table": row.slug,
        "pattern": pattern,
        "location": root,
        "files": len(files),
        "rows": sum(meta.num_rows for _uri, _size, meta in footers),
        "bytes": sum(size for _uri, size, _meta in footers),
        "columns": [*arrow_schema.names, *keys],
        "partitioned_by": keys,
        "_footers": footers,
        "_hive": hive,
        "_arrow_schema": arrow_schema,
        "_partition_types": partition_types,
    }


def _public(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def _build_iceberg_metadata(catalog: LotIcebergCatalog, plan: dict[str, Any]) -> str:
    """Write Iceberg metadata referencing the existing files; returns the metadata location."""
    from pyiceberg.catalog.sql import SqlCatalog

    arrow_schema: pa.Schema | None = plan.get("_arrow_schema")
    if arrow_schema is not None:
        file_schema: Schema = catalog._convert_schema_if_needed(arrow_schema, 2)  # type: ignore[assignment]
    else:
        file_schema = plan.get("_iceberg_schema") or Schema(
            NestedField(1, "id", LongType(), required=False)
        )
    next_id = file_schema.highest_field_id + 1
    partition_fields = [
        NestedField(next_id + i, key, plan["_partition_types"][key], required=False)
        for i, key in enumerate(plan["partitioned_by"])
    ]
    schema = Schema(*file_schema.fields, *partition_fields)

    # Staged in a throwaway catalog: the LoT catalog only sees the finished, verified table.
    staging = SqlCatalog("lot-migration", uri="sqlite:///:memory:", **storage_properties())
    staging.create_namespace("staging")
    staged = staging.create_table(
        "staging.migrating",
        schema=schema,
        location=plan["location"],
        partition_spec=PartitionSpec(),
        properties={"lot.migrated-from": "parquet"},
    )
    if not plan.get("_footers"):
        return staged.metadata_location

    # Field ids were reassigned by create_table, so resolve everything from the staged schema.
    schema = staged.schema()
    with staged.update_spec() as spec_update:
        for key in plan["partitioned_by"]:
            spec_update.add_identity(key)
    staged = staging.load_table("staging.migrating")
    schema = staged.schema()
    data_columns = schema.select(*arrow_schema.names)
    stats_plan = compute_statistics_plan(schema, staged.metadata.properties)
    column_mapping = parquet_path_to_id_mapping(data_columns)

    with staged.transaction() as txn:
        # The files carry no Iceberg field ids; readers resolve columns by name.
        txn.set_properties(
            **{TableProperties.DEFAULT_NAME_MAPPING: schema.name_mapping.model_dump_json()}
        )
        with txn._append_snapshot_producer(
            {"lot.source": "migration:parquet-to-iceberg"}
        ) as append:
            for (uri, size, parquet_metadata), hive in zip(
                plan["_footers"], plan["_hive"], strict=True
            ):
                values = dict(hive)
                stats = data_file_statistics_from_parquet_metadata(
                    parquet_metadata=parquet_metadata,
                    stats_columns=stats_plan,
                    parquet_column_mapping=column_mapping,
                )
                append.append_data_file(
                    DataFile.from_args(
                        content=DataFileContent.DATA,
                        file_path=uri,
                        file_format=FileFormat.PARQUET,
                        partition=Record(
                            *[
                                _partition_value(values[key], plan["_partition_types"][key])
                                for key in plan["partitioned_by"]
                            ]
                        ),
                        file_size_in_bytes=size,
                        sort_order_id=None,
                        spec_id=staged.metadata.default_spec_id,
                        equality_ids=None,
                        key_metadata=None,
                        **stats.to_serialized_dict(),
                    )
                )
    return staging.load_table("staging.migrating").metadata_location


def _local_path(uri: str) -> str:
    return uri[len("file://") :] if uri.startswith("file://") else uri


def verify_migration(plan: dict[str, Any], metadata_location: str) -> dict[str, Any]:
    """Compare the Parquet view and the new Iceberg view of the table with DuckDB."""
    if plan.get("files", 0) == 0:
        return {
            "ok": True,
            "parquet_rows": 0,
            "iceberg_rows": 0,
            "footer_rows": 0,
            "missing_columns": [],
            "type_changes": {},
        }
    if warehouse_root().startswith("file://"):
        import duckdb

        con = duckdb.connect()
    else:
        import ai_service

        con = ai_service.create_duckdb_connection(load_vss=False, register_ai=False)
    try:
        ensure_iceberg_loaded(con)
        parquet_scan = table_read_scan(_local_path(plan["pattern"]), union_by_name=True)
        iceberg_scan = table_read_scan(
            None, table_format="iceberg", metadata_location=_local_path(metadata_location)
        )
        parquet_rows = con.execute(f"SELECT count(*) FROM {parquet_scan}").fetchone()[0]
        iceberg_rows = con.execute(f"SELECT count(*) FROM {iceberg_scan}").fetchone()[0]
        parquet_cols = {
            r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {parquet_scan}").fetchall()
        }
        iceberg_cols = {
            r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {iceberg_scan}").fetchall()
        }
    finally:
        con.close()
    missing = sorted(set(parquet_cols) - set(iceberg_cols))
    type_changes = {
        name: f"{parquet_cols[name]} -> {iceberg_cols[name]}"
        for name in parquet_cols
        if name in iceberg_cols and parquet_cols[name] != iceberg_cols[name]
    }
    ok = parquet_rows == iceberg_rows == plan["rows"] and not missing
    return {
        "ok": ok,
        "parquet_rows": parquet_rows,
        "iceberg_rows": iceberg_rows,
        "footer_rows": plan["rows"],
        "missing_columns": missing,
        "type_changes": type_changes,
    }


def migrate_table(
    db: Session, row: CatalogTable, *, dry_run: bool = False, verify: bool = True
) -> dict[str, Any]:
    """Migrate one Parquet table to Iceberg in place. Never raises for per-table problems."""
    result: dict[str, Any] = {"table_id": str(row.id), "table": row.slug, "dry_run": dry_run}
    try:
        plan = plan_table_migration(db, row)
    except MigrationSkipped as exc:
        return {**result, "status": "skipped", "reason": str(exc)}
    except Exception as exc:
        logger.exception("Planning Iceberg migration failed for %s", row.slug)
        return {**result, "status": "failed", "error": str(exc)}

    result.update(_public(plan))
    if dry_run:
        return {**result, "status": "planned"}

    catalog = system_catalog(db, owner_workspace_of(row))
    existing_meta = plan.get("_existing_metadata")
    if existing_meta:
        try:
            original_pattern = row.s3_path_pattern
            table = catalog.adopt_table(row, existing_meta)
            db.refresh(row)
            state = dict(row.iceberg.maintenance or {})
            state["migrated_from"] = {
                "format": "parquet",
                "s3_path_pattern": original_pattern,
                "files": 0,
                "rows": 0,
                "snapshot_id": (
                    str(table.metadata.current_snapshot_id)
                    if table.metadata.current_snapshot_id
                    else None
                ),
                "migrated_at": datetime.now(UTC).isoformat(),
            }
            row.iceberg.maintenance = state
            flag_modified(row.iceberg, "maintenance")
            db.commit()
            return {
                **result,
                "status": "migrated",
                "metadata_location": existing_meta,
                "snapshot_id": (
                    str(table.metadata.current_snapshot_id)
                    if table.metadata.current_snapshot_id
                    else None
                ),
            }
        except Exception as exc:
            db.rollback()
            return {**result, "status": "failed", "error": str(exc)}

    metadata_dir = f"{plan['location']}/metadata"
    try:
        metadata_location = _build_iceberg_metadata(catalog, plan)
        if verify:
            verification = verify_migration(plan, metadata_location)
            result["verification"] = verification
            if not verification["ok"]:
                raise RuntimeError(
                    "verification failed: "
                    f"parquet={verification['parquet_rows']} iceberg={verification['iceberg_rows']} "
                    f"footers={verification['footer_rows']} missing={verification['missing_columns']}"
                )
        original_pattern = row.s3_path_pattern
        table = catalog.adopt_table(row, metadata_location)
    except Exception as exc:
        db.rollback()
        # Nothing was adopted: remove the staged metadata so the table stays plain Parquet.
        staged = [uri for uri, _modified in _list_files(metadata_dir)]
        if staged:
            _delete_files(_IoHolder(catalog._io({}, metadata_dir)), staged)
        logger.warning("Iceberg migration of %s failed: %s", row.slug, exc)
        return {**result, "status": "failed", "error": str(exc)}

    db.refresh(row)
    state = dict(row.iceberg.maintenance or {})
    state["migrated_from"] = {
        "format": "parquet",
        "s3_path_pattern": original_pattern,
        "files": plan["files"],
        "rows": plan["rows"],
        "snapshot_id": str(table.metadata.current_snapshot_id),
        "migrated_at": datetime.now(UTC).isoformat(),
    }
    row.iceberg.maintenance = state
    flag_modified(row.iceberg, "maintenance")
    db.commit()
    return {
        **result,
        "status": "migrated",
        "metadata_location": table.metadata_location,
        "snapshot_id": str(table.metadata.current_snapshot_id),
    }


class _IoHolder:
    """Adapter so `_delete_files` can be reused with a bare FileIO."""

    def __init__(self, io: Any) -> None:
        self.io = io


def rollback_migration(db: Session, row: CatalogTable) -> dict[str, Any]:
    """Undo a zero-copy migration while the table is still at its migration snapshot.

    Only the catalog row changes back; the original Parquet files were never modified. Once
    the table has received Iceberg commits, rolling back would silently lose them, so it is
    refused.
    """
    if row.format != "iceberg" or not row.iceberg:
        raise ValueError("Table is not an Iceberg table")
    origin = (row.iceberg.maintenance or {}).get("migrated_from")
    if not origin or not origin.get("s3_path_pattern"):
        raise ValueError("Table was not created by a Parquet-to-Iceberg migration")
    if str(row.iceberg.current_snapshot_id) != str(origin.get("snapshot_id")):
        raise ValueError(
            "Table has new commits since it was migrated; rolling back would discard them"
        )
    location = row.iceberg.location
    io_holder = _IoHolder(system_catalog(db, owner_workspace_of(row))._io({}, location))
    row.format = "parquet"
    row.s3_path_pattern = origin["s3_path_pattern"]
    db.delete(row.iceberg)
    db.commit()
    db.refresh(row)
    removed = _delete_files(
        io_holder, [uri for uri, _modified in _list_files(f"{location.rstrip('/')}/metadata")]
    )
    return {
        "table": row.slug,
        "status": "rolled_back",
        "s3_path_pattern": row.s3_path_pattern,
        "metadata_files_removed": removed,
    }


def migrate_catalog(
    db: Session,
    catalog: Catalog,
    *,
    table: str | None = None,
    dry_run: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    """Migrate every Parquet table of a catalog (or one table by slug/name/id)."""
    rows: list[CatalogTable] = []
    wanted = (table or "").strip().lower()
    for schema in catalog.schemas:
        for row in schema.tables:
            if wanted and wanted not in (
                row.slug.lower(),
                row.name.lower(),
                str(row.id),
                f"{schema.slug}.{row.slug}".lower(),
            ):
                continue
            rows.append(row)
    if wanted and not rows:
        raise ValueError(f"Table '{table}' was not found in catalog '{catalog.slug}'")

    results = []
    for row in rows:
        outcome = migrate_table(db, row, dry_run=dry_run, verify=verify)
        outcome["schema"] = row.schema.slug
        results.append(outcome)
    summary: dict[str, int] = {}
    for outcome in results:
        summary[outcome["status"]] = summary.get(outcome["status"], 0) + 1
    return {
        "catalog": catalog.slug,
        "catalog_id": str(catalog.id),
        "dry_run": dry_run,
        "verify": verify,
        "summary": summary,
        "tables": results,
    }
