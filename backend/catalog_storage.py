"""Object-storage cleanup for catalogs being permanently deleted."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from storage_service import MINIO_BUCKET, _human_size, get_s3_client

_GLOB_CHARS = re.compile(r"[*?\[]")


def s3_prefix_for_pattern(pattern: str, bucket: str = MINIO_BUCKET) -> str | None:
    """Return the key prefix (ending in "/") covered by a table's s3_path_pattern.

    "s3://datalake/prod/rev/charges/**/*.parquet" -> "prod/rev/charges/"
    Returns None when the pattern is outside the bucket or has no directory part,
    so a malformed pattern can never resolve to the whole bucket.
    """
    pattern = pattern.strip()
    scheme = f"s3://{bucket}/"
    if not pattern.startswith(scheme):
        return None
    key = pattern[len(scheme) :]
    glob = _GLOB_CHARS.search(key)
    if glob:
        key = key[: glob.start()]
    if "/" not in key:
        return None
    prefix = key.rsplit("/", 1)[0].strip("/")
    return f"{prefix}/" if prefix else None


def is_pattern_in_workspace(pattern: str, workspace_slug: str, bucket: str = MINIO_BUCKET) -> bool:
    """Check if a table's S3 path pattern resides strictly within the workspace's storage prefix."""
    clean_slug = (workspace_slug or "default").strip("/")
    ws_scheme = f"s3://{bucket}/workspaces/{clean_slug}/"
    return pattern.strip().startswith(ws_scheme)


def validate_catalog_patterns_for_workspace(
    patterns: list[str],
    workspace_slug: str,
    bucket: str = MINIO_BUCKET,
) -> list[str]:
    """Return any patterns that attempt to escape or reside outside the workspace's jail."""
    invalid: list[str] = []
    for pat in patterns:
        if not is_pattern_in_workspace(pat, workspace_slug, bucket):
            invalid.append(pat)
    return invalid


def compute_prefix_storage_metrics(
    prefix: str,
    bucket: str = MINIO_BUCKET,
    client: Any | None = None,
) -> dict[str, Any]:
    """Compute aggregated storage metrics (object count, total bytes, top subprefixes) for a prefix."""
    s3 = client or get_s3_client()
    clean_prefix = prefix.lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"

    total_objects = 0
    total_bytes = 0
    latest_modified: datetime | None = None
    subprefixes: dict[str, dict[str, Any]] = {}

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=clean_prefix):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            if key == clean_prefix or key.endswith("/"):
                continue

            size = item.get("Size", 0)
            last_mod = item.get("LastModified", datetime.now(UTC))

            total_objects += 1
            total_bytes += size

            if latest_modified is None or last_mod > latest_modified:
                latest_modified = last_mod

            # Group by immediate child folder under prefix
            rel_path = key[len(clean_prefix) :] if key.startswith(clean_prefix) else key
            sub = rel_path.split("/")[0] if "/" in rel_path else "root"
            if sub not in subprefixes:
                subprefixes[sub] = {"objects": 0, "size": 0}
            subprefixes[sub]["objects"] += 1
            subprefixes[sub]["size"] += size

    return {
        "prefix": clean_prefix,
        "bucket": bucket,
        "total_objects": total_objects,
        "total_bytes": total_bytes,
        "total_size_human": _human_size(total_bytes),
        "last_modified": latest_modified.isoformat() if latest_modified else None,
        "subprefixes": {
            k: {
                "objects": v["objects"],
                "size": v["size"],
                "size_human": _human_size(v["size"]),
            }
            for k, v in subprefixes.items()
        },
    }


def _overlaps(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def prefixes_to_delete(own_patterns: list[str], other_patterns: list[str]) -> list[str]:
    """Prefixes owned only by the catalog being purged.

    Skips any prefix that overlaps one used by a table in another catalog, so
    shared raw data (e.g. ingest output several catalogs point at) is kept.
    """
    others = [p for p in map(s3_prefix_for_pattern, other_patterns) if p]
    result: list[str] = []
    for prefix in filter(None, map(s3_prefix_for_pattern, own_patterns)):
        if prefix in result or any(_overlaps(prefix, o) for o in others):
            continue
        result.append(prefix)
    return result


def delete_prefix(prefix: str, bucket: str = MINIO_BUCKET, client=None) -> int:
    """Delete every object under prefix. Returns the number deleted."""
    s3 = client or get_s3_client()
    deleted = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            deleted += len(objects)
    return deleted
