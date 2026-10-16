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


def _collect_s3_paths(db, catalog_id: str) -> list[str]:
    from sqlalchemy import text

    rows = db.execute(
        text(
            "SELECT s3_path_pattern FROM catalog_tables "
            "WHERE schema_id IN ("
            "  SELECT id FROM catalog_schemas WHERE catalog_id = :cid"
            ") AND s3_path_pattern IS NOT NULL"
        ),
        {"cid": catalog_id},
    ).fetchall()
    return [r[0] for r in rows if r[0]]


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

        for row in due:
            catalog_id, catalog_name = str(row[0]), row[1]
            print(f"Purging catalog '{catalog_name}' ({catalog_id})")

            # Collect S3 paths before cascade-deleting DB records
            s3_paths = _collect_s3_paths(db, catalog_id)

            # Delete S3 data first; if this fails the task retries before touching DB
            for path in s3_paths:
                # Patterns look like s3://bucket/a/b/**/*.parquet; list by key prefix "a/b/"
                prefix = _prefix_for_pattern(path)
                if prefix is None:
                    print(f"  Skipping unrecognised path pattern: {path}")
                    continue
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
