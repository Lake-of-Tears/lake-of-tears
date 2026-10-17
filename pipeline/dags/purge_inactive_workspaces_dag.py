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


def purge_inactive_workspaces():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_url = os.getenv("DATABASE_URL", "postgresql://lake_auth:lake_auth@postgres:5432/lake_auth")
    engine = create_engine(db_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        due = db.execute(
            text(
                "SELECT id, name FROM workspaces "
                "WHERE status = 'inactive' "
                "  AND scheduled_purge_at IS NOT NULL "
                "  AND scheduled_purge_at <= NOW()"
            )
        ).fetchall()

        print(f"Found {len(due)} workspace(s) due for purge")

        for row in due:
            workspace_id, workspace_name = str(row[0]), row[1]
            print(f"Purging workspace '{workspace_name}' ({workspace_id})")

            # Patterns look like s3://bucket/a/b/**/*.parquet; list by key prefix "a/b/".
            # Skip prefixes that overlap tables in catalogs this workspace does not own.
            own_rows = db.execute(
                text(
                    "SELECT t.s3_path_pattern FROM catalog_tables t "
                    "JOIN catalog_schemas s ON t.schema_id = s.id "
                    "JOIN catalogs c ON s.catalog_id = c.id "
                    "WHERE c.owner_workspace_id = :wid AND t.s3_path_pattern IS NOT NULL"
                ),
                {"wid": workspace_id},
            ).fetchall()
            other_rows = db.execute(
                text(
                    "SELECT t.s3_path_pattern FROM catalog_tables t "
                    "JOIN catalog_schemas s ON t.schema_id = s.id "
                    "JOIN catalogs c ON s.catalog_id = c.id "
                    "WHERE c.owner_workspace_id <> :wid AND t.s3_path_pattern IS NOT NULL"
                ),
                {"wid": workspace_id},
            ).fetchall()
            others = [p for p in (_prefix_for_pattern(r[0]) for r in other_rows) if p]

            seen: set[str] = set()
            for (pattern,) in own_rows:
                prefix = _prefix_for_pattern(pattern)
                if prefix is None:
                    print(f"  Skipping unrecognised path pattern: {pattern}")
                    continue
                if prefix in seen:
                    continue
                if any(prefix.startswith(o) or o.startswith(prefix) for o in others):
                    print(f"  Skipping prefix shared with another workspace: {prefix}")
                    continue
                seen.add(prefix)
                try:
                    _delete_s3_prefix(prefix)
                    print(f"  Deleted S3 prefix: {prefix}")
                except Exception as exc:
                    print(f"  S3 delete failed for {prefix}: {exc}")
                    raise  # trigger retry

            # Hard-delete workspace (cascades to catalogs, schemas, tables, access grants, members)
            db.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
            db.commit()
            print(f"  Workspace '{workspace_name}' deleted")

    finally:
        db.close()


with DAG(
    dag_id="purge_inactive_workspaces",
    default_args=default_args,
    description="Hourly purge of inactive workspaces past their grace period",
    schedule_interval="0 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace", "maintenance"],
) as dag:
    PythonOperator(
        task_id="purge_inactive_workspaces",
        python_callable=purge_inactive_workspaces,
    )
