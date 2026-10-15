from __future__ import annotations

import io
import logging
import math
import os
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlunparse

import boto3
from botocore.config import Config

logger = logging.getLogger("lake.backend.storage.service")

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


def build_breadcrumbs(prefix: str) -> list[dict[str, str]]:
    """Build breadcrumb path list from S3 prefix."""
    crumbs = [{"name": "datalake", "prefix": ""}]
    if not prefix:
        return crumbs

    parts = [p for p in prefix.strip("/").split("/") if p]
    accum = ""
    for part in parts:
        accum += f"{part}/"
        crumbs.append({"name": part, "prefix": accum})
    return crumbs


def get_parent_prefix(prefix: str) -> str | None:
    """Get the immediate parent prefix or None if at root."""
    if not prefix or prefix == "/":
        return None
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
) -> dict[str, Any]:
    """Browse hierarchical folders and objects in a bucket prefix."""
    s3 = s3_client or get_s3_client()
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
        folders.append(
            {
                "name": name,
                "prefix": full_pfx,
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

        files.append(
            {
                "key": key,
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

    return {
        "bucket": bucket,
        "prefix": clean_prefix,
        "parent_prefix": get_parent_prefix(clean_prefix),
        "breadcrumbs": build_breadcrumbs(clean_prefix),
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
) -> dict[str, Any]:
    """Retrieve S3 metadata and extract detailed Parquet schema/metrics via PyArrow."""
    s3 = s3_client or get_s3_client()
    clean_key = key.lstrip("/")

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

    return {
        "bucket": bucket,
        "key": clean_key,
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
    }


def get_download_url(
    bucket: str = MINIO_BUCKET,
    key: str = "",
    expires_in: int = 3600,
    s3_client: Any | None = None,
) -> str:
    """Generate a presigned S3 GET URL for browser download.

    Replaces internal Docker endpoint host with MINIO_PUBLIC_ENDPOINT if necessary.
    """
    s3 = s3_client or get_s3_client()
    clean_key = key.lstrip("/")

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
) -> tuple[Generator[bytes, None, None], dict[str, str]]:
    """Stream object content as fallback if presigned URL is unreachable."""
    s3 = s3_client or get_s3_client()
    clean_key = key.lstrip("/")
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
) -> dict[str, Any]:
    """Delete an S3 object."""
    s3 = s3_client or get_s3_client()
    clean_key = key.lstrip("/")
    s3.delete_object(Bucket=bucket, Key=clean_key)
    return {"deleted": True, "bucket": bucket, "key": clean_key}


def get_bucket_summary(
    bucket: str = MINIO_BUCKET,
    s3_client: Any | None = None,
) -> dict[str, Any]:
    """Compute overall bucket stats and top-level prefix breakdown."""
    try:
        s3 = s3_client or get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        total_objects = total_size = 0
        prefix_stats: dict[str, dict] = {}
        source_last_modified: dict[str, datetime | None] = {s: None for s in SOURCES}

        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                size: int = obj["Size"]
                last_mod: datetime = obj["LastModified"]

                total_objects += 1
                total_size += size

                top = key.split("/")[0] if "/" in key else key
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
                    if f"/{src}/" in key or key.startswith(f"{src}/"):
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
