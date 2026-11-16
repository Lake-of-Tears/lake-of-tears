"""PyIceberg client for Lake of Tears' native Iceberg REST catalog.

Used by pipelines (`storage_writer`), Lake Asset Bundle tasks and notebooks to perform ACID
appends / overwrites with schema evolution. Every commit goes through the LoT REST catalog
(`<backend>/api/v1/iceberg`), which enforces workspace RBAC and regenerates the table's TOON
metadata views.

    from pipeline.shared import iceberg_client          # pipelines
    from lake_of_tears import iceberg as iceberg_client  # notebooks / SDK

    iceberg_client.write_table(df, "main_lake.silver.orders", mode="append")

Identical copies live in pipeline/shared/ and backend/lake_of_tears/ (separate containers).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

MODE_APPEND = "append"
MODE_OVERWRITE = "overwrite"
_COMMIT_RETRIES = 4


def backend_url() -> str:
    return (
        os.environ.get("LOT_BACKEND_URL")
        or os.environ.get("BACKEND_URL")
        or os.environ.get("LAKE_API_BASE")
        or os.environ.get("LAKE_URL")
        or "http://lake-of-tears-backend:8000"
    ).rstrip("/")


def resolve_workspace(workspace: str | None = None) -> str:
    raw = workspace or os.environ.get("WORKSPACE_SLUG") or os.environ.get("WORKSPACE") or "default"
    return raw.replace("\\", "/").strip("/ ") or "default"


def mint_task_token(workspace: str, ttl_seconds: int = 7200) -> str | None:
    """Mint a short-lived workspace-scoped task token when the platform signing key is present.

    Only pipeline workers hold `AUTH_SECRET_KEY`; everywhere else pass a personal access token.
    """
    secret = os.environ.get("AUTH_SECRET_KEY")
    if not secret:
        return None

    def _b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    bundle = os.environ.get("BUNDLE_NAME") or "storage-writer"
    pipeline = os.environ.get("PIPELINE_NAME") or ""
    task = os.environ.get("TASK_NAME") or ""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(
        json.dumps(
            {
                "typ": "lot_task",
                "sub": f"pipeline:{bundle}/{pipeline}/{task}",
                "workspace_slug": workspace,
                "bundle_name": bundle,
                "pipeline_name": pipeline,
                "task_name": task,
                "iat": now,
                "exp": now + ttl_seconds,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = _b64(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + signature).decode("ascii")


def resolve_token(workspace: str) -> str | None:
    return (
        os.environ.get("LOT_TASK_TOKEN")
        or os.environ.get("LAKE_TOKEN")
        or os.environ.get("LAKE_API_KEY")
        or os.environ.get("LAKE_AUTH_TOKEN")
        or mint_task_token(workspace)
    )


def s3_properties() -> dict[str, str]:
    """FileIO settings for data files; the catalog never vends storage credentials."""
    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    if not endpoint.startswith(("http://", "https://")):
        secure = os.environ.get("MINIO_SECURE", "false").lower() in ("true", "1")
        endpoint = f"{'https' if secure else 'http'}://{endpoint}"
    return {
        "s3.endpoint": endpoint,
        "s3.access-key-id": os.environ.get("MINIO_ACCESS_KEY", ""),
        "s3.secret-access-key": os.environ.get("MINIO_SECRET_KEY", ""),
        "s3.region": os.environ.get("AWS_REGION") or os.environ.get("MINIO_REGION", "us-east-1"),
        "s3.path-style-access": "true",
    }


def get_catalog(workspace: str | None = None, **properties: str) -> Any:
    """Connect PyIceberg to the LoT REST catalog for a workspace."""
    from pyiceberg.catalog import load_catalog

    ws = resolve_workspace(workspace)
    props: dict[str, str] = {
        "type": "rest",
        "uri": f"{backend_url()}/api/v1/iceberg",
        "warehouse": ws,
        **s3_properties(),
    }
    token = resolve_token(ws)
    if token:
        props["token"] = token
    props.update(properties)
    return load_catalog(f"lot-{ws}", **props)


def to_arrow(data: Any) -> Any:
    """Accept a PyArrow table, pandas DataFrame, DuckDB relation or list of dict rows."""
    import pyarrow as pa

    if isinstance(data, pa.Table):
        table = data
    elif hasattr(data, "to_arrow_table"):
        table = data.to_arrow_table()
    elif hasattr(data, "to_arrow"):
        table = data.to_arrow()
    elif isinstance(data, list):
        table = pa.Table.from_pylist(data)
    else:
        table = pa.Table.from_pandas(data, preserve_index=False)

    fields = []
    for field in table.schema:
        t = field.type
        if (
            pa.types.is_uint8(t)
            or pa.types.is_uint16(t)
            or pa.types.is_int8(t)
            or pa.types.is_int16(t)
        ):
            t = pa.int32()
        elif pa.types.is_uint32(t):
            t = pa.int64()
        elif pa.types.is_uint64(t):
            t = pa.decimal128(20, 0)
        elif pa.types.is_null(t):
            t = pa.string()
        elif pa.types.is_timestamp(t) and t.unit != "us":
            t = pa.timestamp("us", tz=t.tz)
        elif pa.types.is_float16(t):
            t = pa.float32()
        fields.append(pa.field(field.name, t, nullable=True))
    target = pa.schema(fields)
    return table if target.equals(table.schema) else table.cast(target, safe=False)


def parse_identifier(
    identifier: str | tuple[str, ...] | None = None,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    table: str | None = None,
) -> tuple[str, str, str]:
    if identifier:
        parts = tuple(identifier.split(".")) if isinstance(identifier, str) else tuple(identifier)
    else:
        parts = tuple(p for p in (catalog, schema, table) if p)
    if len(parts) != 3 or not all(parts):
        raise ValueError("Iceberg tables are addressed as <catalog>.<schema>.<table>")
    return parts[0], parts[1], parts[2]


def write_table(
    data: Any,
    identifier: str | tuple[str, ...] | None = None,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    table: str | None = None,
    workspace: str | None = None,
    mode: str = MODE_APPEND,
    partition_by: list[str] | None = None,
    evolve_schema: bool = True,
    snapshot_properties: dict[str, str] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """ACID append / overwrite into an Iceberg table, creating it (and its schema) if missing.

    New columns are added through schema evolution (`union_by_name`). Appends that lose a
    commit race are retried against the refreshed table.
    """
    from pyiceberg.exceptions import CommitFailedException, NoSuchTableError

    if mode not in (MODE_APPEND, MODE_OVERWRITE):
        raise ValueError(f"Unsupported write mode '{mode}' (use 'append' or 'overwrite')")
    ident = parse_identifier(identifier, catalog=catalog, schema=schema, table=table)
    arrow = to_arrow(data)
    cat = client or get_catalog(workspace)
    props = dict(snapshot_properties or {})

    created = False
    try:
        tbl = cat.load_table(ident)
    except NoSuchTableError:
        cat.create_namespace_if_not_exists(ident[:2])
        tbl = cat.create_table(ident, schema=arrow.schema)
        created = True
        if partition_by:
            with tbl.update_spec() as spec:
                for column in partition_by:
                    spec.add_identity(column)

    last_error: Exception | None = None
    for attempt in range(_COMMIT_RETRIES):
        try:
            if evolve_schema and not created:
                with tbl.update_schema() as update:
                    update.union_by_name(arrow.schema)
            if mode == MODE_OVERWRITE:
                tbl.overwrite(arrow, snapshot_properties=props)
            else:
                tbl.append(arrow, snapshot_properties=props)
            last_error = None
            break
        except CommitFailedException as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
            tbl = cat.load_table(ident)
    if last_error is not None:
        raise last_error

    snapshot = tbl.current_snapshot()
    return {
        "table": ".".join(ident),
        "mode": mode,
        "created": created,
        "rows_written": arrow.num_rows,
        "snapshot_id": snapshot.snapshot_id if snapshot else None,
        "location": tbl.metadata.location,
        "metadata_location": tbl.metadata_location,
    }


def read_table(
    identifier: str | tuple[str, ...],
    *,
    workspace: str | None = None,
    snapshot_id: int | None = None,
    row_filter: str | None = None,
    client: Any = None,
) -> Any:
    """Read an Iceberg table (optionally a past snapshot) into a PyArrow table."""
    cat = client or get_catalog(workspace)
    tbl = cat.load_table(parse_identifier(identifier))
    kwargs: dict[str, Any] = {}
    if snapshot_id is not None:
        kwargs["snapshot_id"] = snapshot_id
    if row_filter:
        kwargs["row_filter"] = row_filter
    return tbl.scan(**kwargs).to_arrow()
