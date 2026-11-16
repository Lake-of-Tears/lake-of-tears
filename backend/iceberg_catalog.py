"""Native Apache Iceberg catalog for Lake of Tears.

`LotIcebergCatalog` is a PyIceberg `MetastoreCatalog` backed by the existing LoT PostgreSQL
catalog (`catalogs` → `catalog_schemas` → `catalog_tables`) plus the `iceberg_tables` pointer
table. It is used two ways:

* in-process, by the backend (create-table flow, maintenance, migration, restore), and
* over HTTP, by `iceberg_rest_router`, which exposes it as a standard Iceberg REST catalog.

Identifiers are `(catalog_slug, schema_slug, table_slug)`; the Iceberg namespace is the
two-level `(catalog_slug, schema_slug)`. Every table location is jailed to the owning
workspace's storage prefix.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
from catalog_acl import caller_access
from models import Catalog as CatalogRow
from models import CatalogSchema, CatalogTable, IcebergTable, Workspace
from pyiceberg.catalog import Catalog, MetastoreCatalog, PropertiesUpdateSummary
from pyiceberg.exceptions import (
    CommitFailedException,
    ForbiddenError,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    NoSuchViewError,
    TableAlreadyExistsError,
)
from pyiceberg.io import FileIO, load_file_io
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.serializers import FromInputFile
from pyiceberg.table import CommitTableResponse, Table, TableProperties
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import TableMetadata, new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import TableRequirement, TableUpdate
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FixedType,
    FloatType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from storage_service import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
)
from table_scan import FORMAT_ICEBERG

logger = logging.getLogger("lake.backend.iceberg.catalog")

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_METADATA_FILE_RE = re.compile(r"^(?:v(\d+)|(\d+)-[0-9a-fA-F-]+)(?:\.gz)?\.metadata\.json$")
_PROTECTED_SCHEMAS = {"bronze", "silver", "gold"}


# ── Storage configuration ─────────────────────────────────────────────────────


def warehouse_root() -> str:
    """Root URI under which every workspace jail lives (`<root>/workspaces/<slug>/…`)."""
    return (os.getenv("ICEBERG_WAREHOUSE_ROOT") or f"s3://{MINIO_BUCKET}").rstrip("/")


def storage_properties() -> dict[str, str]:
    """PyIceberg FileIO properties for the platform's MinIO / S3 object store."""
    endpoint = MINIO_ENDPOINT
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"{'https' if MINIO_SECURE else 'http'}://{endpoint}"
    return {
        "s3.endpoint": endpoint,
        "s3.access-key-id": MINIO_ACCESS_KEY,
        "s3.secret-access-key": MINIO_SECRET_KEY,
        "s3.region": os.getenv("AWS_REGION") or os.getenv("MINIO_REGION", "us-east-1"),
        "s3.force-virtual-addressing": "false",
    }


def workspace_prefix(workspace_slug: str) -> str:
    return f"{warehouse_root()}/workspaces/{(workspace_slug or 'default').strip('/')}/"


def default_table_location(
    workspace_slug: str, catalog_slug: str, schema_slug: str, table_slug: str
) -> str:
    return f"{workspace_prefix(workspace_slug)}{catalog_slug}/{schema_slug}/{table_slug}"


def is_within_workspace(location: str, workspace_slug: str) -> bool:
    """True when `location` (normalized) resides strictly inside the workspace's storage jail."""
    loc = (location or "").strip()
    scheme, sep, rest = loc.partition("://")
    if not sep or any(part in ("..", ".") for part in rest.split("/")):
        return False
    return loc.startswith(workspace_prefix(workspace_slug))


def _safe_identifier(name: str | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip())
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def _matches(slug: str | None, wanted: str) -> bool:
    """Match an Iceberg identifier part against a LoT slug (hyphen/underscore-insensitive)."""
    if slug is None:
        return False
    return slug == wanted or _safe_identifier(slug).lower() == _safe_identifier(wanted).lower()


# ── Iceberg <-> LoT column definition mapping ─────────────────────────────────


def iceberg_type_to_sql(field_type: IcebergType) -> str:
    """Render an Iceberg type using the DuckDB type names used in `catalog_tables.column_defs`."""
    if isinstance(field_type, BooleanType):
        return "BOOLEAN"
    if isinstance(field_type, IntegerType):
        return "INTEGER"
    if isinstance(field_type, LongType):
        return "BIGINT"
    if isinstance(field_type, FloatType):
        return "FLOAT"
    if isinstance(field_type, DoubleType):
        return "DOUBLE"
    if isinstance(field_type, DecimalType):
        return f"DECIMAL({field_type.precision},{field_type.scale})"
    if isinstance(field_type, DateType):
        return "DATE"
    if isinstance(field_type, TimeType):
        return "TIME"
    if isinstance(field_type, TimestamptzType):
        return "TIMESTAMPTZ"
    if isinstance(field_type, TimestampType):
        return "TIMESTAMP"
    if isinstance(field_type, StringType):
        return "VARCHAR"
    if isinstance(field_type, UUIDType):
        return "UUID"
    if isinstance(field_type, BinaryType | FixedType):
        return "BLOB"
    return str(field_type).upper()


def sql_type_to_iceberg(type_name: str | None) -> IcebergType:
    """Map a DuckDB / LoT column type name to the closest Iceberg primitive type."""
    raw = (type_name or "VARCHAR").strip().upper()
    base = re.sub(r"\(.*\)", "", raw).strip()
    if base in ("BOOLEAN", "BOOL"):
        return BooleanType()
    if base in ("TINYINT", "SMALLINT", "INTEGER", "INT", "INT4", "INT2", "INT1", "UTINYINT"):
        return IntegerType()
    if base in ("BIGINT", "INT8", "LONG", "HUGEINT", "UBIGINT", "UINTEGER", "USMALLINT"):
        return LongType()
    if base in ("FLOAT", "REAL", "FLOAT4"):
        return FloatType()
    if base in ("DOUBLE", "FLOAT8", "DOUBLE PRECISION"):
        return DoubleType()
    if base in ("DECIMAL", "NUMERIC"):
        m = re.search(r"\((\d+)\s*(?:,\s*(\d+))?\)", raw)
        if m:
            return DecimalType(int(m.group(1)), int(m.group(2) or 0))
        return DecimalType(18, 3)
    if base == "DATE":
        return DateType()
    if base == "TIME":
        return TimeType()
    if base in ("TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"):
        return TimestamptzType()
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return TimestampType()
    if base == "UUID":
        return UUIDType()
    if base in ("BLOB", "BYTEA", "BINARY", "VARBINARY"):
        return BinaryType()
    return StringType()


def schema_from_column_defs(column_defs: list[dict[str, Any]] | None) -> Schema:
    """Build an Iceberg schema (fresh field IDs) from LoT column definitions."""
    fields: list[NestedField] = []
    for idx, col in enumerate(column_defs or [], start=1):
        name = str(col.get("name") or "").strip()
        if not name:
            continue
        fields.append(
            NestedField(
                field_id=idx,
                name=name,
                field_type=sql_type_to_iceberg(col.get("type")),
                required=False,
                doc=(col.get("description") or None),
            )
        )
    if not fields:
        raise ValueError("An Iceberg table needs at least one column")
    return Schema(*fields)


def column_defs_from_schema(
    schema: Schema, existing: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Project an Iceberg schema onto LoT column defs, keeping curated descriptions by name."""
    described = {
        str(c.get("name")): c.get("description")
        for c in (existing or [])
        if isinstance(c, dict) and c.get("description")
    }
    return [
        {
            "name": f.name,
            "type": iceberg_type_to_sql(f.field_type),
            "description": described.get(f.name) or f.doc or None,
            "nullable": not f.required,
        }
        for f in schema.fields
    ]


# ── Auto-detection ────────────────────────────────────────────────────────────


def _split_location(location: str) -> tuple[str, str, str]:
    scheme, _, rest = location.partition("://")
    bucket, _, key = rest.partition("/")
    return scheme, bucket, key


def _detection_s3_client() -> Any:
    """S3 client with short timeouts: detection runs inline in catalog requests."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=storage_properties()["s3.endpoint"],
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 1},
        ),
    )


def detect_iceberg_metadata(location: str | None, s3_client: Any = None) -> str | None:
    """Return the newest `metadata/*.metadata.json` under a table location, or None.

    Accepts a table root (`s3://b/p/t`, `s3://b/p/t/`) or a LoT glob pattern
    (`s3://b/p/t/**/*.parquet`). Both Hadoop-style `v3.metadata.json` and catalog-style
    `00003-<uuid>.metadata.json` names are recognised; the highest version wins.
    """
    if not location:
        return None
    root = location.strip()
    glob = re.search(r"[*?\[]", root)
    if glob:
        root = root[: glob.start()].rsplit("/", 1)[0]
    elif root.lower().endswith(".parquet"):
        root = root.rsplit("/", 1)[0]
    root = root.rstrip("/")
    for suffix in ("/data", "/metadata"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    scheme, bucket, key = _split_location(root)

    names: list[tuple[str, datetime | float]] = []
    if scheme in ("s3", "s3a", "s3n"):
        if s3_client is None:
            s3_client = _detection_s3_client()
        prefix = f"{key}/metadata/"
        try:
            for page in s3_client.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix
            ):
                for item in page.get("Contents", []):
                    names.append((item["Key"][len(prefix) :], item.get("LastModified") or 0.0))
        except Exception as exc:
            logger.debug("Iceberg auto-detection could not list %s: %s", root, exc)
            return None
    elif scheme == "file" or not scheme:
        local = root[len("file://") :] if scheme == "file" else root
        meta_dir = os.path.join(local, "metadata")
        if os.path.isdir(meta_dir):
            for entry in os.listdir(meta_dir):
                names.append((entry, os.path.getmtime(os.path.join(meta_dir, entry))))
    else:
        return None

    best: tuple[int, str] | None = None
    for name, _mtime in names:
        m = _METADATA_FILE_RE.match(name)
        if not m:
            continue
        version = int(m.group(1) or m.group(2))
        if best is None or (version, name) > best:
            best = (version, name)
    return f"{root}/metadata/{best[1]}" if best else None


# ── Catalog ───────────────────────────────────────────────────────────────────


class LotIcebergCatalog(MetastoreCatalog):
    """PyIceberg catalog over the LoT PostgreSQL catalog, scoped to one workspace and caller."""

    def __init__(
        self,
        db: Session,
        workspace: Workspace,
        user: Any = None,
        *,
        role: str | None = None,
        enforce_acl: bool = True,
        **properties: str,
    ) -> None:
        super().__init__(f"lot-{workspace.slug}", **{**storage_properties(), **properties})
        self.db = db
        self.workspace = workspace
        self.user = user
        self.role = (role or "").lower() or None
        self.enforce_acl = enforce_acl and user is not None

    # ── Access control ────────────────────────────────────────────────────────

    def _access(self, catalog: CatalogRow) -> str | None:
        if not self.enforce_acl:
            return "owner"
        owned = str(catalog.owner_workspace_id) == str(self.workspace.id)
        if getattr(self.user, "_is_task_token", False) is True:
            # Pipeline task tokens are jailed to their workspace; tasks are how pipelines write.
            return "write" if owned else None
        access = caller_access(self.db, catalog, self.user, ws=self.workspace)
        if access in ("owner", "write") and self.role in ("read", "viewer"):
            # Personal access tokens can cap the caller below their membership role.
            return "read"
        return access

    def _require_write(self, catalog: CatalogRow, what: str) -> None:
        if self._access(catalog) not in ("owner", "write"):
            raise ForbiddenError(f"Write access to catalog '{catalog.slug}' is required to {what}")

    # ── Row resolution ────────────────────────────────────────────────────────

    def _visible_catalogs(self) -> list[CatalogRow]:
        rows = (
            self.db.query(CatalogRow)
            .filter(CatalogRow.deleted_at.is_(None))
            .order_by(CatalogRow.created_at)
            .all()
        )
        return [c for c in rows if self._access(c) is not None]

    def _catalog_row(self, slug: str) -> CatalogRow:
        row = (
            self.db.query(CatalogRow)
            .filter(CatalogRow.slug == slug, CatalogRow.deleted_at.is_(None))
            .first()
        )
        if row is None or self._access(row) is None:
            row = next((c for c in self._visible_catalogs() if _matches(c.slug, slug)), None)
        if row is None:
            raise NoSuchNamespaceError(f"Namespace does not exist: {slug}")
        return row

    def _schema_row(self, namespace: str | Identifier) -> tuple[CatalogRow, CatalogSchema]:
        parts = Catalog.identifier_to_tuple(namespace)
        if len(parts) != 2:
            raise NoSuchNamespaceError(
                f"Namespace does not exist: {'.'.join(parts)} (expected <catalog>.<schema>)"
            )
        catalog = self._catalog_row(parts[0])
        schema = next((s for s in catalog.schemas if s.slug == parts[1]), None) or next(
            (s for s in catalog.schemas if _matches(s.slug, parts[1])), None
        )
        if schema is None:
            raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(parts)}")
        return catalog, schema

    def _table_row(
        self, identifier: str | Identifier
    ) -> tuple[CatalogRow, CatalogSchema, CatalogTable]:
        parts = Catalog.identifier_to_tuple(identifier)
        if len(parts) > 3:
            parts = parts[-3:]
        if len(parts) != 3:
            raise NoSuchTableError(
                f"Table does not exist: {'.'.join(parts)} (expected <catalog>.<schema>.<table>)"
            )
        try:
            catalog, schema = self._schema_row(parts[:2])
        except NoSuchNamespaceError as exc:
            raise NoSuchTableError(f"Table does not exist: {'.'.join(parts)}") from exc
        tables = [t for t in schema.tables if t.format == FORMAT_ICEBERG and t.iceberg]
        table = next((t for t in tables if t.slug == parts[2]), None) or next(
            (t for t in tables if _matches(t.slug, parts[2])), None
        )
        if table is None:
            raise NoSuchTableError(f"Table does not exist: {'.'.join(parts)}")
        return catalog, schema, table

    @staticmethod
    def _identifier_of(catalog: CatalogRow, schema: CatalogSchema, table_slug: str) -> Identifier:
        return (catalog.slug, schema.slug, table_slug)

    def _owner_slug(self, catalog: CatalogRow) -> str:
        owner = getattr(catalog, "owner_workspace", None)
        return (getattr(owner, "slug", None) or self.workspace.slug) or "default"

    def _assert_jailed(self, catalog: CatalogRow, *locations: str) -> None:
        slug = self._owner_slug(catalog)
        for loc in locations:
            if not is_within_workspace(loc, slug):
                raise ForbiddenError(
                    f"Location '{loc}' is outside the storage of workspace '{slug}' "
                    f"({workspace_prefix(slug)})"
                )

    def _io(self, metadata_properties: Properties, location: str) -> FileIO:
        return load_file_io({**self.properties, **metadata_properties}, location)

    def _read_metadata(self, metadata_location: str) -> TableMetadata:
        io = load_file_io(self.properties, metadata_location)
        return FromInputFile.table_metadata(io.new_input(metadata_location))

    def _to_table(self, catalog: CatalogRow, schema: CatalogSchema, table: CatalogTable) -> Table:
        metadata_location = table.iceberg.metadata_location
        try:
            metadata = self._read_metadata(metadata_location)
        except FileNotFoundError as exc:
            raise NoSuchTableError(
                f"Metadata file is missing for {catalog.slug}.{schema.slug}.{table.slug}: "
                f"{metadata_location}"
            ) from exc
        return Table(
            identifier=self._identifier_of(catalog, schema, table.slug),
            metadata=metadata,
            metadata_location=metadata_location,
            io=self._io(metadata.properties, metadata_location),
            catalog=self,
        )

    # ── Post-commit synchronisation (pointer cache, column defs, TOON views) ──

    def _after_commit(self, table: CatalogTable, metadata: TableMetadata) -> None:
        """Refresh derived state after the pointer swap. Never fails the commit itself."""
        ice = table.iceberg
        try:
            ice.table_uuid = str(metadata.table_uuid)
            ice.location = metadata.location
            ice.current_schema_id = metadata.current_schema_id
            ice.current_snapshot_id = metadata.current_snapshot_id
            table.column_defs = column_defs_from_schema(metadata.schema(), table.column_defs)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.warning("Post-commit catalog sync failed for table %s: %s", table.slug, exc)
        try:
            # Lets long-lived readers resolve the current version from the table root.
            hint = f"{metadata.location.rstrip('/')}/metadata/version-hint.text"
            stem = ice.metadata_location.rsplit("/", 1)[-1].removesuffix(".metadata.json")
            with self._io(metadata.properties, hint).new_output(hint).create(overwrite=True) as f:
                f.write(stem.encode("utf-8"))
        except Exception as exc:
            logger.warning("Could not write version hint for table %s: %s", table.slug, exc)
        try:
            import iceberg_toon

            iceberg_toon.refresh_table_toon(self.db, table, catalog=self)
        except Exception as exc:
            self.db.rollback()
            logger.warning("On-commit TOON generation failed for table %s: %s", table.slug, exc)

    # ── Tables ────────────────────────────────────────────────────────────────

    def _new_rows(
        self,
        schema: CatalogSchema,
        name: str,
        metadata: TableMetadata,
        metadata_location: str,
        description: str | None = None,
    ) -> CatalogTable:
        row = self._pending_row(schema, name)
        if row is None:
            row = CatalogTable(
                schema_id=schema.id, name=name, slug=name.lower(), format=FORMAT_ICEBERG
            )
            self.db.add(row)
        if description:
            row.description = description
        row.s3_path_pattern = metadata.location.rstrip("/") + "/"
        row.column_defs = column_defs_from_schema(metadata.schema(), row.column_defs)
        if row.iceberg:
            row.iceberg.table_uuid = str(metadata.table_uuid)
            row.iceberg.location = metadata.location
            row.iceberg.metadata_location = metadata_location
            row.iceberg.current_schema_id = metadata.current_schema_id
            row.iceberg.current_snapshot_id = metadata.current_snapshot_id
        else:
            row.iceberg = IcebergTable(
                table_uuid=str(metadata.table_uuid),
                location=metadata.location,
                metadata_location=metadata_location,
                current_schema_id=metadata.current_schema_id,
                current_snapshot_id=metadata.current_snapshot_id,
            )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise TableAlreadyExistsError(
                f"Table already exists: {schema.slug}.{name.lower()}"
            ) from exc
        self.db.refresh(schema)
        return row

    @staticmethod
    def _pending_row(schema: CatalogSchema, name: str) -> CatalogTable | None:
        """A declared Iceberg table (e.g. from a Lake Asset Bundle) awaiting its first commit,
        or an existing row whose metadata files are missing or uninitialized."""
        for t in schema.tables:
            if not (_matches(t.slug, name) and t.format == FORMAT_ICEBERG):
                continue
            if not t.iceberg or not t.iceberg.metadata_location:
                return t
            try:
                if not detect_iceberg_metadata(t.iceberg.location or ""):
                    return t
            except Exception:
                return t
        return None

    def _validate_new_table(
        self, identifier: str | Identifier
    ) -> tuple[CatalogRow, CatalogSchema, str]:
        parts = Catalog.identifier_to_tuple(identifier)
        if len(parts) != 3:
            raise NoSuchNamespaceError(
                f"Namespace does not exist: {'.'.join(parts[:-1])} (expected <catalog>.<schema>)"
            )
        catalog, schema = self._schema_row(parts[:2])
        self._require_write(catalog, "create tables")
        name = parts[2].strip()
        if not _SLUG_RE.match(name):
            raise ValueError(f"Invalid table name '{name}': use letters, digits, '_' and '-' only")
        pending = self._pending_row(schema, name)
        if any(_matches(t.slug, name) and t is not pending for t in schema.tables):
            raise TableAlreadyExistsError(
                f"Table already exists: {catalog.slug}.{schema.slug}.{name.lower()}"
            )
        return catalog, schema, name

    def resolve_new_table_location(
        self, catalog: CatalogRow, schema: CatalogSchema, name: str, location: str | None
    ) -> str:
        resolved = (location or "").rstrip("/") or default_table_location(
            self._owner_slug(catalog), catalog.slug, schema.slug, name.lower()
        )
        self._assert_jailed(catalog, resolved)
        return resolved

    def create_table(
        self,
        identifier: str | Identifier,
        schema: Schema | pa.Schema,
        location: str | None = None,
        partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
        sort_order: SortOrder = UNSORTED_SORT_ORDER,
        properties: Properties = EMPTY_DICT,
        description: str | None = None,
    ) -> Table:
        catalog, schema_row, name = self._validate_new_table(identifier)
        iceberg_schema: Schema = self._convert_schema_if_needed(  # type: ignore[assignment]
            schema,
            int(
                properties.get(
                    TableProperties.FORMAT_VERSION, TableProperties.DEFAULT_FORMAT_VERSION
                )
            ),
        )
        location = self.resolve_new_table_location(catalog, schema_row, name, location)
        pending = self._pending_row(schema_row, name)
        if pending is None and detect_iceberg_metadata(location):
            raise TableAlreadyExistsError(
                f"Location {location} already contains an Iceberg table "
                "(a dropped table awaiting purge in Trash, or one to register instead)"
            )
        metadata = new_table_metadata(
            location=location,
            schema=iceberg_schema,
            partition_spec=partition_spec,
            sort_order=sort_order,
            properties=properties,
        )
        provider = load_location_provider(table_location=location, table_properties=properties)
        metadata_location = provider.new_table_metadata_file_location()
        self._write_metadata(metadata, self._io(properties, metadata_location), metadata_location)
        row = self._new_rows(schema_row, name, metadata, metadata_location, description)
        self._after_commit(row, metadata)
        return self._to_table(catalog, schema_row, row)

    def register_table(
        self, identifier: str | Identifier, metadata_location: str, overwrite: bool = False
    ) -> Table:
        if overwrite:
            raise NotImplementedError("`overwrite` isn't supported")
        catalog, schema_row, name = self._validate_new_table(identifier)
        self._assert_jailed(catalog, metadata_location)
        metadata = self._read_metadata(metadata_location)
        self._assert_jailed(catalog, metadata.location)
        row = self._new_rows(schema_row, name, metadata, metadata_location)
        self._after_commit(row, metadata)
        return self._to_table(catalog, schema_row, row)

    def adopt_table(self, table: CatalogTable, metadata_location: str) -> Table:
        """Switch an existing catalog table row to Iceberg, pointing at existing metadata.

        Used by auto-detection and by the zero-copy Parquet migration: the row keeps its id,
        description, grants and vector index; only the read path changes.
        """
        schema_row = table.schema
        catalog = schema_row.catalog
        self._require_write(catalog, "convert tables to Iceberg")
        self._assert_jailed(catalog, metadata_location)
        metadata = self._read_metadata(metadata_location)
        self._assert_jailed(catalog, metadata.location)
        table.format = FORMAT_ICEBERG
        table.s3_path_pattern = metadata.location.rstrip("/") + "/"
        table.iceberg = IcebergTable(
            table_uuid=str(metadata.table_uuid),
            location=metadata.location,
            metadata_location=metadata_location,
            current_schema_id=metadata.current_schema_id,
            current_snapshot_id=metadata.current_snapshot_id,
        )
        self.db.commit()
        self._after_commit(table, metadata)
        return self._to_table(catalog, schema_row, table)

    def load_table(self, identifier: str | Identifier) -> Table:
        return self._to_table(*self._table_row(identifier))

    def load_table_row(self, table: CatalogTable) -> Table:
        """Load the PyIceberg table for an already-resolved (and authorized) catalog row."""
        if table.format != FORMAT_ICEBERG or not table.iceberg:
            raise NoSuchTableError(f"Table '{table.slug}' is not an Iceberg table")
        return self._to_table(table.schema.catalog, table.schema, table)

    def table_exists(self, identifier: str | Identifier) -> bool:
        try:
            self._table_row(identifier)
            return True
        except NoSuchTableError:
            return False

    def commit_table(
        self,
        table: Table,
        requirements: tuple[TableRequirement, ...],
        updates: tuple[TableUpdate, ...],
    ) -> CommitTableResponse:
        return self.commit_identifier(table.name(), requirements, updates)

    def commit_identifier(
        self,
        identifier: Identifier,
        requirements: tuple[TableRequirement, ...],
        updates: tuple[TableUpdate, ...],
    ) -> CommitTableResponse:
        """Validate requirements, apply updates and atomically swap the metadata pointer."""
        row: CatalogTable | None
        try:
            catalog, schema_row, row = self._table_row(identifier)
            current: Table | None = self._to_table(catalog, schema_row, row)
            self._require_write(catalog, "commit to tables")
        except NoSuchTableError:
            # Staged create (`stage-create` + assert-create): first commit creates the table.
            catalog, schema_row, name = self._validate_new_table(identifier)
            row, current = None, None

        staged = self._update_and_stage_table(
            current, self._identifier_of(catalog, schema_row, identifier[-1]), requirements, updates
        )
        if current and staged.metadata == current.metadata:
            return CommitTableResponse(
                metadata=current.metadata, metadata_location=current.metadata_location
            )
        self._assert_jailed(catalog, staged.metadata.location, staged.metadata_location)
        self._write_metadata(staged.metadata, staged.io, staged.metadata_location)

        if row is None or current is None:
            row = self._new_rows(schema_row, name, staged.metadata, staged.metadata_location)
        else:
            # Optimistic concurrency: swap the pointer only if nobody committed since we read it.
            result = self.db.execute(
                update(IcebergTable)
                .where(
                    IcebergTable.table_id == row.id,
                    IcebergTable.metadata_location == current.metadata_location,
                )
                .values(
                    metadata_location=staged.metadata_location,
                    previous_metadata_location=current.metadata_location,
                    updated_at=datetime.now(UTC),
                )
            )
            if result.rowcount < 1:
                self.db.rollback()
                raise CommitFailedException(
                    f"Table has been updated by another process: {'.'.join(identifier)}"
                )
            self.db.commit()
            self.db.refresh(row.iceberg)

        self._after_commit(row, staged.metadata)
        return CommitTableResponse(
            metadata=staged.metadata, metadata_location=staged.metadata_location
        )

    def drop_table(self, identifier: str | Identifier) -> None:
        """Soft-delete into the catalog Trash (recoverable with UNDROP until the purge TTL)."""
        import catalog_trash_service

        catalog, schema_row, row = self._table_row(identifier)
        self._require_write(catalog, "drop tables")
        owner = getattr(catalog, "owner_workspace", None) or self.workspace
        catalog_trash_service.soft_delete_table(
            db=self.db,
            table=row,
            schema=schema_row,
            catalog=catalog,
            workspace=owner,
            user_id=getattr(self.user, "id", None)
            if not getattr(self.user, "_is_task_token", False)
            else None,
        )
        self.db.commit()

    def purge_table(self, identifier: str | Identifier) -> None:
        # Purging is deliberately routed through Trash so a mistaken DROP … PURGE is recoverable.
        self.drop_table(identifier)

    def rename_table(
        self, from_identifier: str | Identifier, to_identifier: str | Identifier
    ) -> Table:
        catalog, _schema_row, row = self._table_row(from_identifier)
        to_parts = Catalog.identifier_to_tuple(to_identifier)
        to_catalog, to_schema, name = self._validate_new_table(to_parts)
        self._require_write(catalog, "rename tables")
        if to_catalog.id != catalog.id:
            raise ValueError("Tables cannot be renamed across catalogs")
        row.schema_id = to_schema.id
        row.name = name
        row.slug = name.lower()
        self.db.commit()
        self.db.refresh(row)
        return self._to_table(to_catalog, to_schema, row)

    def list_tables(self, namespace: str | Identifier) -> list[Identifier]:
        catalog, schema_row = self._schema_row(namespace)
        return [
            self._identifier_of(catalog, schema_row, t.slug)
            for t in schema_row.tables
            if t.format == FORMAT_ICEBERG and t.iceberg
        ]

    # ── Namespaces ────────────────────────────────────────────────────────────

    def list_namespaces(self, namespace: str | Identifier = ()) -> list[Identifier]:
        parts = Catalog.identifier_to_tuple(namespace) if namespace else ()
        if not parts:
            return [(c.slug,) for c in self._visible_catalogs()]
        if len(parts) == 1:
            catalog = self._catalog_row(parts[0])
            return [(catalog.slug, s.slug) for s in catalog.schemas]
        self._schema_row(parts)
        return []

    def namespace_exists(self, namespace: str | Identifier) -> bool:
        parts = Catalog.identifier_to_tuple(namespace)
        try:
            if len(parts) == 1:
                self._catalog_row(parts[0])
            else:
                self._schema_row(parts)
            return True
        except NoSuchNamespaceError:
            return False

    def create_namespace(
        self, namespace: str | Identifier, properties: Properties = EMPTY_DICT
    ) -> None:
        parts = Catalog.identifier_to_tuple(namespace)
        if len(parts) != 2:
            raise ValueError(
                "Namespaces are <catalog>.<schema>; create the catalog itself in Lake of Tears"
            )
        catalog = self._catalog_row(parts[0])
        self._require_write(catalog, "create schemas")
        name = parts[1].strip()
        if not _SLUG_RE.match(name):
            raise ValueError(f"Invalid namespace name '{name}'")
        if any(_matches(s.slug, name) for s in catalog.schemas):
            raise NamespaceAlreadyExistsError(f"Namespace already exists: {'.'.join(parts)}")
        slug = name.lower()
        self.db.add(
            CatalogSchema(
                catalog_id=catalog.id,
                name=name,
                slug=slug,
                description=properties.get("comment") or properties.get("description"),
                tier=slug if slug in _PROTECTED_SCHEMAS else "custom",
            )
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise NamespaceAlreadyExistsError(
                f"Namespace already exists: {'.'.join(parts)}"
            ) from exc
        self.db.refresh(catalog)

    def drop_namespace(self, namespace: str | Identifier) -> None:
        catalog, schema_row = self._schema_row(namespace)
        self._require_write(catalog, "drop schemas")
        if schema_row.slug in _PROTECTED_SCHEMAS or schema_row.tier in _PROTECTED_SCHEMAS:
            raise ForbiddenError(f"Core medallion schema '{schema_row.slug}' cannot be dropped")
        if schema_row.tables:
            raise NamespaceNotEmptyError(f"Namespace {catalog.slug}.{schema_row.slug} is not empty")
        self.db.delete(schema_row)
        self.db.commit()
        self.db.refresh(catalog)

    def load_namespace_properties(self, namespace: str | Identifier) -> Properties:
        parts = Catalog.identifier_to_tuple(namespace)
        if len(parts) == 1:
            catalog = self._catalog_row(parts[0])
            props = {"location": f"{workspace_prefix(self._owner_slug(catalog))}{catalog.slug}"}
            if catalog.description:
                props["comment"] = catalog.description
            return props
        catalog, schema_row = self._schema_row(parts)
        props = {
            "location": (
                f"{workspace_prefix(self._owner_slug(catalog))}{catalog.slug}/{schema_row.slug}"
            ),
            "tier": schema_row.tier or "custom",
        }
        if schema_row.description:
            props["comment"] = schema_row.description
        return props

    def update_namespace_properties(
        self,
        namespace: str | Identifier,
        removals: set[str] | None = None,
        updates: Properties = EMPTY_DICT,
    ) -> PropertiesUpdateSummary:
        catalog, schema_row = self._schema_row(namespace)
        self._require_write(catalog, "update schemas")
        self._check_for_overlap(removals=removals, updates=updates)
        removed, updated, missing = [], [], []
        for key in removals or set():
            if key in ("comment", "description") and schema_row.description:
                schema_row.description = None
                removed.append(key)
            else:
                missing.append(key)
        for key, value in (updates or {}).items():
            if key in ("comment", "description"):
                schema_row.description = str(value)
                updated.append(key)
            else:
                missing.append(key)
        self.db.commit()
        return PropertiesUpdateSummary(removed=removed, updated=updated, missing=missing)

    # ── Views (not supported) ─────────────────────────────────────────────────

    def list_views(self, namespace: str | Identifier) -> list[Identifier]:
        self._schema_row(namespace)
        return []

    def view_exists(self, identifier: str | Identifier) -> bool:
        return False

    def load_view(self, identifier: str | Identifier) -> Any:
        raise NoSuchViewError(f"View does not exist: {identifier}")

    def drop_view(self, identifier: str | Identifier) -> None:
        raise NoSuchViewError(f"View does not exist: {identifier}")

    def register_view(self, identifier: str | Identifier, metadata_location: str) -> Any:
        raise NotImplementedError("Iceberg views are not supported by the Lake of Tears catalog")

    def create_view(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Iceberg views are not supported by the Lake of Tears catalog")


# ── In-process helpers ────────────────────────────────────────────────────────


def system_catalog(db: Session, workspace: Workspace) -> LotIcebergCatalog:
    """Catalog for backend-internal work whose caller was already authorized by the API layer."""
    return LotIcebergCatalog(db, workspace, enforce_acl=False)


def owner_workspace_of(table: CatalogTable) -> Workspace:
    return table.schema.catalog.owner_workspace


def load_iceberg_table(db: Session, table: CatalogTable) -> Table:
    """Load the PyIceberg `Table` behind an Iceberg-format catalog row (pre-authorized)."""
    return system_catalog(db, owner_workspace_of(table)).load_table_row(table)
