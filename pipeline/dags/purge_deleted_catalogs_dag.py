from __future__ import annotations

import os
from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator

default_args = {
    "owner": "airflow",
    "retries": 3,
    "retry_delay": __import__("datetime").timedelta(minutes=5),
}

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")


def _load_patterns(db) -> list[tuple[str, str]]:
    """Return (catalog_id, s3_path_pattern) for every catalog table with a pattern."""
    from sqlalchemy import text

    rows = db.execute(
        text(
            "SELECT s.catalog_id, t.s3_path_pattern FROM catalog_tables t "
            "JOIN catalog_schemas s ON t.schema_id = s.id "
            "WHERE t.s3_path_pattern IS NOT NULL"
        )
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows if r[1]]


def _overlaps(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def _prefix_for_pattern(pattern: str) -> str | None:
    import re

    scheme = f"s3://{MINIO_BUCKET}/"
    pattern = pattern.strip()
    if not pattern.startswith(scheme):
        return None
    key = pattern[len(scheme) :]
    glob = re.search(r"[*?\[]", key)
    if glob:
        key = key[: glob.start()]
    if "/" not in key:
        return None
    prefix = key.rsplit("/", 1)[0].strip("/")
    return f"{prefix}/" if prefix else None


def _delete_s3_prefix(prefix: str) -> None:
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{MINIO_ENDPOINT}",
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=MINIO_BUCKET, Prefix=prefix):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=MINIO_BUCKET, Delete={"Objects": objects})


def purge_deleted_catalogs():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_url = os.getenv("DATABASE_URL", "postgresql://lake_auth:lake_auth@postgres:5432/lake_auth")
    engine = create_engine(db_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        due = db.execute(
            text(
                "SELECT id, name FROM catalogs "
                "WHERE scheduled_purge_at IS NOT NULL "
                "  AND scheduled_purge_at <= NOW() "
                "  AND deleted_at IS NOT NULL"
            )
        ).fetchall()

        print(f"Found {len(due)} catalog(s) due for purge")

        # Catalogs purged together may share data with each other; only catalogs that
        # survive this run (including soft-deleted ones still in their grace period) count.
        due_ids = {str(r[0]) for r in due}
        patterns = _load_patterns(db)
        kept_prefixes = [
            p
            for p in (_prefix_for_pattern(pat) for cid, pat in patterns if cid not in due_ids)
            if p
        ]

        for row in due:
            catalog_id, catalog_name = str(row[0]), row[1]
            print(f"Purging catalog '{catalog_name}' ({catalog_id})")

            # Delete S3 data first; if this fails the task retries before touching DB.
            # Patterns look like s3://bucket/a/b/**/*.parquet; list by key prefix "a/b/".
            # Skip prefixes that overlap tables in catalogs NOT being purged in this run.
            seen: set[str] = set()
            for cid, pattern in patterns:
                if cid != catalog_id:
                    continue
                prefix = _prefix_for_pattern(pattern)
                if prefix is None:
                    print(f"  Skipping unrecognised path pattern: {pattern}")
                    continue
                if prefix in seen:
                    continue
                if any(_overlaps(prefix, o) for o in kept_prefixes):
                    print(f"  Skipping prefix shared with another catalog: {prefix}")
                    continue
                seen.add(prefix)
                try:
                    _delete_s3_prefix(prefix)
                    print(f"  Deleted S3 prefix: {prefix}")
                except Exception as exc:
                    print(f"  S3 delete failed for {prefix}: {exc}")
                    raise  # trigger retry

            # Hard-delete DB record (cascade removes schemas, tables, access grants)
            db.execute(text("DELETE FROM catalogs WHERE id = :id"), {"id": catalog_id})
            db.commit()
            print(f"  DB record deleted for '{catalog_name}'")

    finally:
        db.close()


with DAG(
    dag_id="purge_deleted_catalogs",
    default_args=default_args,
    description="Daily purge of soft-deleted catalogs past their grace period",
    schedule_interval="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["catalog", "maintenance"],
) as dag:
    PythonOperator(
        task_id="purge_deleted_catalogs",
        python_callable=purge_deleted_catalogs,
    )
