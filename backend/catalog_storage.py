"""Object-storage cleanup for catalogs being permanently deleted."""

from __future__ import annotations

import re

from storage_service import MINIO_BUCKET, get_s3_client

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
