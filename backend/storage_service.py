from __future__ import annotations

import io
import json
import logging
import math
import os
import re
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlunparse

import boto3
from botocore.config import Config

logger = logging.getLogger("lake.backend.storage.service")


# ── Storage Custom Exceptions ─────────────────────────────────────────────────


class StoragePathError(ValueError):
    """Raised when a storage path contains invalid characters or formatting."""

    pass


class StorageAccessDeniedError(PermissionError):
    """Raised when an operation attempts to access storage outside the assigned workspace jail."""

    pass


# ── Configuration ─────────────────────────────────────────────────────────────

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_PUBLIC_ENDPOINT = os.getenv("MINIO_PUBLIC_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1")

SOURCES = ["stripe", "shopify", "hubspot", "weather", "truenas", "jellyfin"]
SOURCE_LABELS = {
    "stripe": "Stripe Charges",
    "shopify": "Shopify Orders",
    "hubspot": "HubSpot Contacts",
    "weather": "Open-Meteo Weather",
    "truenas": "TrueNAS Storage",
    "jellyfin": "Jellyfin Activity",
}


def _human_size(num_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if num_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    exp = min(int(math.log(max(num_bytes, 1), 1024)), len(units) - 1)
    return f"{num_bytes / (1024**exp):.1f} {units[exp]}"


def _source_status(last_modified: datetime | None) -> str:
    if last_modified is None:
        return "red"
    age = datetime.now(UTC) - last_modified
    if age <= timedelta(hours=25):
        return "green"
    if age <= timedelta(hours=48):
        return "yellow"
    return "red"


def get_s3_client(endpoint: str | None = None) -> Any:
    """Create a configured Boto3 S3 client."""
    ep = endpoint or MINIO_ENDPOINT
    scheme = "https" if MINIO_SECURE else "http"
    if not ep.startswith("http://") and not ep.startswith("https://"):
        endpoint_url = f"{scheme}://{ep}"
    else:
        endpoint_url = ep

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


# ── Range Reader for Zero-Copy Parquet Footers ─────────────────────────────────


class S3RangeReader(io.RawIOBase):
    """Seekable stream that fetches byte ranges from S3 on-demand.

    Used by PyArrow to read Parquet footers without downloading entire files.
    """

    def __init__(self, s3_client: Any, bucket: str, key: str, total_size: int):
        super().__init__()
        self.s3_client = s3_client
        self.bucket = bucket
        self.key = key
        self.total_size = total_size
        self.pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        elif whence == 2:
            self.pos = self.total_size + offset
        else:
            raise ValueError(f"Invalid whence argument: {whence}")
        self.pos = max(0, min(self.pos, self.total_size))
        return self.pos

    def tell(self) -> int:
        return self.pos

    def readinto(self, b: bytearray | memoryview) -> int:
        length = len(b)
        if self.pos >= self.total_size or length == 0:
            return 0
        end = min(self.pos + length, self.total_size)
        byte_range = f"bytes={self.pos}-{end - 1}"
        try:
            resp = self.s3_client.get_object(
                Bucket=self.bucket,
                Key=self.key,
                Range=byte_range,
            )
            data = resp["Body"].read()
            b[: len(data)] = data
            self.pos += len(data)
            return len(data)
        except Exception as exc:
            logger.warning("S3RangeReader error reading range %s: %s", byte_range, exc)
            return 0


# ── Core Storage Service Operations ──────────────────────────────────────────


def validate_and_jail_path(
    workspace_slug: str,
    path: str,
    is_prefix: bool = False,
) -> str:
    """Validate, sanitize, and strictly jail a storage path or prefix to workspaces/<slug>/.

    Dual-mode: Accepts both relative paths ('raw/charges.parquet') and explicit
    workspace paths ('workspaces/<slug>/raw/charges.parquet').
    Rejects traversal ('..'), invalid characters, or attempts to access root or other workspaces.
    """
    clean_slug = (workspace_slug or "default").replace("\\", "/").strip("/ ")
    if not clean_slug or ".." in clean_slug:
        raise StorageAccessDeniedError("Invalid workspace slug.")
    ws_jail = f"workspaces/{clean_slug}/"

    # Normalize backslashes and strip whitespace
    raw = (path or "").replace("\\", "/").strip()

    # Empty path handling
    if not raw or raw == "/":
        return ws_jail if is_prefix else ws_jail.rstrip("/")

    # Strip leading slash for traversal inspection
    stripped = raw.lstrip("/")
    segments = stripped.split("/")
    for seg in segments:
        if seg in ("..", "."):
            raise StorageAccessDeniedError("Directory traversal ('..') is strictly forbidden.")
        if re.search(r'[\x00-\x1f<>:"|?*]', seg):
            raise StoragePathError(f"Invalid character in path segment: {seg}")

    # Canonicalize multiple slashes
    while "//" in stripped:
        stripped = stripped.replace("//", "/")

    # Check if path explicitly begins with workspaces/
    if stripped == f"workspaces/{clean_slug}" or stripped == f"workspaces/{clean_slug}/":
        return ws_jail if is_prefix else ws_jail.rstrip("/")

    if stripped.startswith("workspaces/"):
        if not stripped.startswith(ws_jail):
            raise StorageAccessDeniedError(
                f"Access denied: Path '{path}' is outside the active workspace jail ('{ws_jail}')."
            )
        resolved = stripped
    else:
        # Relative path within workspace jail
        resolved = f"{ws_jail}{stripped}"

    if is_prefix and not resolved.endswith("/"):
        resolved += "/"

    return resolved


def build_breadcrumbs(
    prefix: str,
    workspace_slug: str | None = None,
) -> list[dict[str, str]]:
    """Build breadcrumb path list from S3 prefix, anchored to workspace root if slug is provided."""
    clean_slug = (workspace_slug or "").strip("/")
    if clean_slug:
        ws_root = f"workspaces/{clean_slug}/"
        crumbs = [{"name": f"Workspace: {clean_slug}", "prefix": ""}]
        rel = prefix
        if rel.startswith(ws_root):
            rel = rel[len(ws_root) :]
        elif rel.startswith(f"workspaces/{clean_slug}"):
            rel = rel[len(f"workspaces/{clean_slug}") :].lstrip("/")

        parts = [p for p in rel.strip("/").split("/") if p]
        accum = ""
        for part in parts:
            accum += f"{part}/"
            crumbs.append({"name": part, "prefix": accum})
        return crumbs

    crumbs = [{"name": "datalake", "prefix": ""}]
    if not prefix:
        return crumbs

    parts = [p for p in prefix.strip("/").split("/") if p]
    accum = ""
    for part in parts:
        accum += f"{part}/"
        crumbs.append({"name": part, "prefix": accum})
    return crumbs


def get_parent_prefix(
    prefix: str,
    workspace_slug: str | None = None,
) -> str | None:
    """Get the immediate parent prefix, returning None if at root or workspace jail root."""
    if not prefix or prefix == "/":
        return None

    clean_slug = (workspace_slug or "").strip("/")
    if clean_slug:
        ws_root = f"workspaces/{clean_slug}/"
        rel = prefix
        if rel.startswith(ws_root):
            rel = rel[len(ws_root) :]
        rel = rel.strip("/")
        if not rel:
            return None
        if "/" not in rel:
            return ""
        return rel.rsplit("/", 1)[0] + "/"

    trimmed = prefix.strip("/")
    if "/" not in trimmed:
        return ""
    return trimmed.rsplit("/", 1)[0] + "/"


def browse_prefix(
    bucket: str = MINIO_BUCKET,
    prefix: str = "",
    delimiter: str = "/",
    limit: int = 100,
    continuation_token: str | None = None,
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> dict[str, Any]:
    """Browse hierarchical folders and objects in a bucket prefix, jailed to workspace if provided."""
    s3 = s3_client or get_s3_client()

    clean_slug = (workspace_slug or "").strip("/")
    ws_jail = f"workspaces/{clean_slug}/" if clean_slug else ""

    if clean_slug:
        clean_prefix = validate_and_jail_path(clean_slug, prefix, is_prefix=True)
    else:
        clean_prefix = prefix.lstrip("/")
        if clean_prefix and not clean_prefix.endswith("/") and delimiter == "/":
            clean_prefix += "/"

    max_keys = max(1, min(limit, 1000))
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Prefix": clean_prefix,
        "Delimiter": delimiter,
        "MaxKeys": max_keys,
    }
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token

    resp = s3.list_objects_v2(**kwargs)

    folders = []
    for cp in resp.get("CommonPrefixes", []):
        full_pfx = cp.get("Prefix", "")
        trimmed = full_pfx.rstrip("/")
        name = trimmed.split("/")[-1] if "/" in trimmed else trimmed
        rel_pfx = full_pfx[len(ws_jail) :] if (ws_jail and full_pfx.startswith(ws_jail)) else full_pfx
        folders.append(
            {
                "name": name,
                "prefix": rel_pfx if clean_slug else full_pfx,
                "full_prefix": full_pfx,
            }
        )

    files = []
    for item in resp.get("Contents", []):
        key = item.get("Key", "")
        # Filter out folder marker objects matching current prefix
        if key == clean_prefix or key.endswith("/"):
            continue
        filename = key.split("/")[-1]
        size = item.get("Size", 0)
        last_mod = item.get("LastModified", datetime.now(UTC))
        etag = item.get("ETag", "").strip('"')
        is_parquet = filename.lower().endswith(".parquet")
        is_notebook = filename.lower().endswith(".ipynb")
        rel_key = key[len(ws_jail) :] if (ws_jail and key.startswith(ws_jail)) else key

        files.append(
            {
                "key": key,
                "rel_key": rel_key,
                "name": filename,
                "size": size,
                "size_human": _human_size(size),
                "last_modified": last_mod.isoformat()
                if hasattr(last_mod, "isoformat")
                else str(last_mod),
                "etag": etag,
                "is_parquet": is_parquet,
                "is_notebook": is_notebook,
            }
        )

    # Sort folders and files alphabetically by name
    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())

    rel_prefix = (
        clean_prefix[len(ws_jail) :]
        if (ws_jail and clean_prefix.startswith(ws_jail))
        else clean_prefix
    )

    return {
        "bucket": bucket,
        "workspace": clean_slug or None,
        "prefix": clean_prefix,
        "rel_prefix": rel_prefix,
        "parent_prefix": get_parent_prefix(clean_prefix, workspace_slug=clean_slug or None),
        "breadcrumbs": build_breadcrumbs(clean_prefix, workspace_slug=clean_slug or None),
        "folders": folders,
        "files": files,
        "total_folders": len(folders),
        "total_files": len(files),
        "is_truncated": resp.get("IsTruncated", False),
        "next_continuation_token": resp.get("NextContinuationToken"),
    }


def get_object_metadata(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> dict[str, Any]:
    """Retrieve S3 metadata and extract detailed Parquet schema/metrics via PyArrow."""
    if workspace_slug:
        clean_key = validate_and_jail_path(workspace_slug, key, is_prefix=False)
    else:
        clean_key = key.lstrip("/")

    s3 = s3_client or get_s3_client()

    head = s3.head_object(Bucket=bucket, Key=clean_key)
    size = head.get("ContentLength", 0)
    last_mod = head.get("LastModified", datetime.now(UTC))
    content_type = head.get("ContentType", "application/octet-stream")
    etag = head.get("ETag", "").strip('"')
    custom_metadata = head.get("Metadata", {})

    filename = clean_key.split("/")[-1]
    is_parquet = filename.lower().endswith(".parquet")
    is_notebook = filename.lower().endswith(".ipynb")

    parquet_info: dict[str, Any] | None = None

    if is_parquet and size > 0:
        try:
            import pyarrow.parquet as pq

            reader = S3RangeReader(s3, bucket, clean_key, size)
            pf = pq.ParquetFile(reader)
            pm = pf.metadata

            columns = []
            num_row_groups = pm.num_row_groups
            for i in range(pm.num_columns):
                col_schema = pf.schema.column(i)
                comp = "UNCOMPRESSED"
                if num_row_groups > 0:
                    try:
                        comp = pm.row_group(0).column(i).compression
                    except Exception:
                        comp = "UNKNOWN"

                # Match Arrow field if available
                arrow_field = None
                try:
                    arrow_field = pf.schema_arrow.field(i)
                except Exception:
                    pass

                columns.append(
                    {
                        "name": col_schema.name,
                        "arrow_type": str(arrow_field.type)
                        if arrow_field
                        else str(col_schema.logical_type or col_schema.physical_type),
                        "physical_type": str(col_schema.physical_type),
                        "logical_type": str(col_schema.logical_type)
                        if col_schema.logical_type
                        else None,
                        "compression": comp,
                        "nullable": arrow_field.nullable if arrow_field else True,
                    }
                )

            parquet_info = {
                "num_rows": pm.num_rows,
                "num_columns": pm.num_columns,
                "num_row_groups": pm.num_row_groups,
                "format_version": pm.format_version,
                "created_by": pm.created_by,
                "serialized_size": pm.serialized_size,
                "columns": columns,
            }
        except Exception as exc:
            logger.warning("Could not parse Parquet metadata for %s: %s", clean_key, exc)
            parquet_info = {"error": str(exc)}

    notebook_info: dict[str, Any] | None = None
    if is_notebook and size > 0 and size < 20 * 1024 * 1024:
        try:
            obj = s3.get_object(Bucket=bucket, Key=clean_key)
            nb_data = json.loads(obj["Body"].read().decode("utf-8"))
            cells = nb_data.get("cells", [])
            metadata = nb_data.get("metadata", {})
            papermill_meta = metadata.get("papermill", {})
            kernelspec = metadata.get("kernelspec", {})

            notebook_info = {
                "nbformat": nb_data.get("nbformat", 4),
                "cells_count": len(cells),
                "kernel_name": kernelspec.get("display_name")
                or kernelspec.get("name")
                or "Python 3",
                "language": kernelspec.get("language") or "python",
                "executed_by": "Papermill" if papermill_meta else None,
                "duration_seconds": papermill_meta.get("duration"),
                "has_exception": bool(papermill_meta.get("exception")),
            }
        except Exception as exc:
            logger.warning("Could not parse notebook metadata for %s: %s", clean_key, exc)
            notebook_info = {"error": str(exc)}

    clean_slug = (workspace_slug or "").strip("/")
    ws_jail = f"workspaces/{clean_slug}/" if clean_slug else ""
    rel_key = (
        clean_key[len(ws_jail) :] if (ws_jail and clean_key.startswith(ws_jail)) else clean_key
    )

    return {
        "bucket": bucket,
        "key": clean_key,
        "rel_key": rel_key,
        "filename": filename,
        "size": size,
        "size_human": _human_size(size),
        "last_modified": last_mod.isoformat() if hasattr(last_mod, "isoformat") else str(last_mod),
        "content_type": content_type,
        "etag": etag,
        "is_parquet": is_parquet,
        "is_notebook": is_notebook,
        "custom_metadata": custom_metadata,
        "parquet_metadata": parquet_info,
        "notebook_metadata": notebook_info,
    }


def get_download_url(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    expires_in: int = 3600,
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> str:
    """Generate a presigned S3 GET URL for browser download.

    Replaces internal Docker endpoint host with MINIO_PUBLIC_ENDPOINT if necessary.
    """
    if workspace_slug:
        clean_key = validate_and_jail_path(workspace_slug, key, is_prefix=False)
    else:
        clean_key = key.lstrip("/")

    s3 = s3_client or get_s3_client()

    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": clean_key},
        ExpiresIn=expires_in,
    )

    # Remap hostname to public endpoint if public endpoint is specified
    if MINIO_PUBLIC_ENDPOINT:
        pub = urlparse(MINIO_PUBLIC_ENDPOINT)
        gen = urlparse(url)
        # Only rewrite if host differs (e.g. minio:9000 vs localhost:9000)
        if gen.netloc != pub.netloc:
            url = urlunparse(
                (
                    pub.scheme or gen.scheme,
                    pub.netloc,
                    gen.path,
                    gen.params,
                    gen.query,
                    gen.fragment,
                )
            )

    return url


def stream_object(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> tuple[Generator[bytes, None, None], dict[str, str]]:
    """Stream object content as fallback if presigned URL is unreachable."""
    if workspace_slug:
        clean_key = validate_and_jail_path(workspace_slug, key, is_prefix=False)
    else:
        clean_key = key.lstrip("/")

    s3 = s3_client or get_s3_client()
    obj = s3.get_object(Bucket=bucket, Key=clean_key)
    body = obj["Body"]
    filename = clean_key.split("/")[-1]
    content_type = obj.get("ContentType", "application/octet-stream")
    content_length = str(obj.get("ContentLength", 0))

    headers = {
        "Content-Type": content_type,
        "Content-Length": content_length,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    def _generator() -> Generator[bytes, None, None]:
        try:
            while chunk := body.read(65536):
                yield chunk
        finally:
            body.close()

    return _generator(), headers


def delete_object(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> dict[str, Any]:
    """Delete an S3 object, strictly jailed to workspace if provided."""
    if workspace_slug:
        clean_key = validate_and_jail_path(workspace_slug, key, is_prefix=False)
    else:
        clean_key = key.lstrip("/")

    s3 = s3_client or get_s3_client()
    s3.delete_object(Bucket=bucket, Key=clean_key)
    return {"deleted": True, "bucket": bucket, "key": clean_key}


def upload_object(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    data: bytes | io.BytesIO | Any = b"",
    content_type: str = "application/octet-stream",
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> dict[str, Any]:
    """Upload an object into S3, jailed to workspace if provided."""
    if workspace_slug:
        clean_key = validate_and_jail_path(workspace_slug, key, is_prefix=False)
    else:
        clean_key = key.lstrip("/")

    s3 = s3_client or get_s3_client()
    s3.put_object(
        Bucket=bucket,
        Key=clean_key,
        Body=data,
        ContentType=content_type,
    )
    clean_slug = (workspace_slug or "").strip("/")
    ws_jail = f"workspaces/{clean_slug}/" if clean_slug else ""
    rel_key = (
        clean_key[len(ws_jail) :] if (ws_jail and clean_key.startswith(ws_jail)) else clean_key
    )
    return {
        "uploaded": True,
        "bucket": bucket,
        "key": clean_key,
        "rel_key": rel_key,
    }


def get_upload_url(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    expires_in: int = 3600,
    content_type: str = "application/octet-stream",
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> str:
    """Generate a presigned S3 PUT URL for browser/client upload."""
    if workspace_slug:
        clean_key = validate_and_jail_path(workspace_slug, key, is_prefix=False)
    else:
        clean_key = key.lstrip("/")

    s3 = s3_client or get_s3_client()
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": clean_key, "ContentType": content_type},
        ExpiresIn=expires_in,
        HttpMethod="PUT",
    )

    if MINIO_PUBLIC_ENDPOINT:
        pub = urlparse(MINIO_PUBLIC_ENDPOINT)
        gen = urlparse(url)
        if gen.netloc != pub.netloc:
            url = urlunparse(
                (
                    pub.scheme or gen.scheme,
                    pub.netloc,
                    gen.path,
                    gen.params,
                    gen.query,
                    gen.fragment,
                )
            )
    return url


def get_bucket_summary(
    bucket: str = MINIO_BUCKET,
    s3_client: Any | None = None,
    workspace_slug: str | None = None,
) -> dict[str, Any]:
    """Compute bucket stats and top-level prefix breakdown, strictly scoped to workspace if provided."""
    try:
        s3 = s3_client or get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        total_objects = total_size = 0
        prefix_stats: dict[str, dict] = {}
        source_last_modified: dict[str, datetime | None] = {s: None for s in SOURCES}

        clean_slug = (workspace_slug or "").strip("/")
        ws_root = f"workspaces/{clean_slug}/" if clean_slug else ""

        paginate_kwargs: dict[str, Any] = {"Bucket": bucket}
        if ws_root:
            paginate_kwargs["Prefix"] = ws_root

        for page in paginator.paginate(**paginate_kwargs):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key == ws_root or key.endswith("/"):
                    continue
                size: int = obj["Size"]
                last_mod: datetime = obj["LastModified"]

                total_objects += 1
                total_size += size

                # Determine relative key to find top subfolder
                rel_key = key[len(ws_root) :] if (ws_root and key.startswith(ws_root)) else key
                top = rel_key.split("/")[0] if "/" in rel_key else "root"
                if top not in prefix_stats:
                    prefix_stats[top] = {"objects": 0, "size": 0, "last_modified": None}
                prefix_stats[top]["objects"] += 1
                prefix_stats[top]["size"] += size
                if (
                    prefix_stats[top]["last_modified"] is None
                    or last_mod > prefix_stats[top]["last_modified"]
                ):
                    prefix_stats[top]["last_modified"] = last_mod

                for src in SOURCES:
                    if f"/{src}/" in rel_key or rel_key.startswith(f"{src}/"):
                        if (
                            source_last_modified[src] is None
                            or last_mod > source_last_modified[src]
                        ):
                            source_last_modified[src] = last_mod

        sources = []
        for src in SOURCES:
            lm = source_last_modified[src]
            sources.append(
                {
                    "key": src,
                    "label": SOURCE_LABELS.get(src, src.title()),
                    "last_modified": lm.isoformat() if lm else None,
                    "last_modified_str": (lm.strftime("%Y-%m-%d %H:%M UTC") if lm else "Never"),
                    "status": _source_status(lm),
                }
            )

        raw = prefix_stats.get("raw", {"objects": 0, "size": 0})
        embeddings = prefix_stats.get("embeddings", {"objects": 0, "size": 0})

        return {
            "ok": True,
            "bucket": bucket,
            "workspace": clean_slug or None,
            "total_objects": total_objects,
            "total_size": _human_size(total_size),
            "raw_objects": raw["objects"],
            "raw_size": _human_size(raw["size"]),
            "embeddings_objects": embeddings["objects"],
            "embeddings_size": _human_size(embeddings["size"]),
            "prefix_stats": {
                k: {
                    "objects": v["objects"],
                    "size": _human_size(v["size"]),
                    "last_modified_str": (
                        v["last_modified"].strftime("%Y-%m-%d %H:%M UTC")
                        if v["last_modified"]
                        else "—"
                    ),
                }
                for k, v in prefix_stats.items()
            },
            "sources": sources,
        }
    except Exception as exc:
        logger.warning("Error fetching bucket stats: %s", exc)
        return {"ok": False, "bucket": bucket, "error": str(exc)}
