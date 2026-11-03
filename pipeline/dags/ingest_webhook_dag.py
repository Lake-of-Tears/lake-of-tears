from __future__ import annotations

import io
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

logger = logging.getLogger("airflow.dag.ingest_webhook")

default_args = {
    "owner": "airflow",
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")

WEBHOOK_COLUMNS = [
    {
        "name": "event_id",
        "type": "VARCHAR",
        "description": "Unique event identifier",
        "deprecated": False,
    },
    {
        "name": "received_at",
        "type": "TIMESTAMP",
        "description": "UTC timestamp event was received",
        "deprecated": False,
    },
    {
        "name": "source",
        "type": "VARCHAR",
        "description": "Origin source identifier",
        "deprecated": False,
    },
    {
        "name": "headers",
        "type": "VARCHAR",
        "description": "Serialized HTTP request headers",
        "deprecated": False,
    },
    {
        "name": "payload",
        "type": "VARCHAR",
        "description": "Serialized webhook payload body",
        "deprecated": False,
    },
]


def _get_s3_client():
    import boto3
    from botocore.config import Config

    ep = MINIO_ENDPOINT
    if not ep.startswith("http://") and not ep.startswith("https://"):
        ep = f"http://{ep}"

    return boto3.client(
        "s3",
        endpoint_url=ep,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _get_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://lake_auth:postgresauthpass@lake-of-tears-postgres:5432/lake_auth"
        if os.getenv("KUBERNETES_SERVICE_HOST")
        else "postgresql://lake_auth:lake_auth@postgres:5432/lake_auth",
    )
    engine = create_engine(db_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    return Session()


def _discover_workspaces_with_buffers(s3) -> list[str]:
    """Find all workspace slugs that have pending raw webhook buffer files."""
    workspaces = set()
    paginator = s3.get_paginator("list_objects_v2")
    prefix = "workspaces/"
    for page in paginator.paginate(Bucket=MINIO_BUCKET, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            ws_prefix = cp.get("Prefix", "")
            # ws_prefix is "workspaces/<slug>/"
            parts = ws_prefix.strip("/").split("/")
            if len(parts) >= 2:
                ws_slug = parts[1]
                # Check if buffer prefix has any keys
                buf_prefix = f"workspaces/{ws_slug}/raw/webhook/buffer/"
                check = s3.list_objects_v2(Bucket=MINIO_BUCKET, Prefix=buf_prefix, MaxKeys=1)
                if check.get("KeyCount", 0) > 0:
                    workspaces.add(ws_slug)
    return sorted(workspaces)


def compact_webhook_buffers(**context) -> list[str]:
    """Compacts buffered raw webhook files into partitioned Parquet files per workspace."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dag_run = context.get("dag_run")
    conf = (dag_run.conf if dag_run else {}) or {}
    target_workspace = conf.get("workspace_slug")

    s3 = _get_s3_client()

    if target_workspace:
        workspaces_to_process = [target_workspace]
    else:
        workspaces_to_process = _discover_workspaces_with_buffers(s3)

    logger.info("Starting webhook compaction for workspaces: %s", workspaces_to_process)
    processed_workspaces = []

    for ws_slug in workspaces_to_process:
        buf_prefix = f"workspaces/{ws_slug}/raw/webhook/buffer/"
        paginator = s3.get_paginator("list_objects_v2")

        # Group buffer files by date partition
        # Key format: workspaces/{ws}/raw/webhook/buffer/year=YYYY/month=MM/day=DD/{event_id}.parquet
        partition_map: dict[str, list[str]] = {}

        for page in paginator.paginate(Bucket=MINIO_BUCKET, Prefix=buf_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".parquet"):
                    continue
                match = re.search(r"(year=\d{4}/month=\d{2}/day=\d{2})", key)
                partition = match.group(1) if match else "year=unknown/month=unknown/day=unknown"
                partition_map.setdefault(partition, []).append(key)

        if not partition_map:
            logger.info("No buffer files to compact for workspace '%s'", ws_slug)
            processed_workspaces.append(ws_slug)
            continue

        for partition, keys in partition_map.items():
            logger.info(
                "Compacting %d buffer files for workspace '%s' partition '%s'",
                len(keys),
                ws_slug,
                partition,
            )

            tables = []
            valid_keys = []
            for key in keys:
                try:
                    resp = s3.get_object(Bucket=MINIO_BUCKET, Key=key)
                    data = resp["Body"].read()
                    tab = pq.read_table(io.BytesIO(data))
                    tables.append(tab)
                    valid_keys.append(key)
                except Exception as exc:
                    logger.warning("Failed to read buffer object %s: %s", key, exc)

            if not tables:
                continue

            combined = pa.concat_tables(tables)
            batch_id = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
            compacted_key = (
                f"workspaces/{ws_slug}/raw/webhook/{partition}/compacted_{batch_id}.parquet"
            )

            out = io.BytesIO()
            pq.write_table(combined, out, compression="SNAPPY")
            out.seek(0)

            s3.put_object(
                Bucket=MINIO_BUCKET,
                Key=compacted_key,
                Body=out.getvalue(),
                ContentType="application/octet-stream",
            )
            logger.info("Wrote compacted parquet to %s", compacted_key)

            # Purge processed buffer files
            for chunk_idx in range(0, len(valid_keys), 1000):
                chunk = valid_keys[chunk_idx : chunk_idx + 1000]
                s3.delete_objects(
                    Bucket=MINIO_BUCKET,
                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
                )
            logger.info("Purged %d buffer objects for partition %s", len(valid_keys), partition)

        processed_workspaces.append(ws_slug)

    # Pass list of processed workspaces to catalog registration task
    context["ti"].xcom_push(key="processed_workspaces", value=processed_workspaces)
    return processed_workspaces


def register_catalog_tables(**context) -> None:
    """Auto-registers or updates the webhook_events table in the workspace catalog."""
    ti = context.get("ti")
    processed_workspaces = ti.xcom_pull(
        key="processed_workspaces", task_ids="compact_webhook_buffers"
    )
    if not processed_workspaces:
        dag_run = context.get("dag_run")
        conf = (dag_run.conf if dag_run else {}) or {}
        ws = conf.get("workspace_slug")
        processed_workspaces = [ws] if ws else []

    if not processed_workspaces:
        logger.info("No workspaces to register catalog tables for.")
        return

    db = _get_db()
    try:
        from sqlalchemy import text

        for ws_slug in processed_workspaces:
            # 1. Resolve workspace id
            ws_row = db.execute(
                text(
                    "SELECT id FROM workspaces WHERE slug = :slug AND status = 'active' AND deleted_at IS NULL"
                ),
                {"slug": ws_slug},
            ).first()
            if not ws_row:
                logger.warning("Workspace '%s' not found for catalog registration", ws_slug)
                continue

            workspace_id = str(ws_row[0])

            # 2. Resolve or find default catalog for workspace
            cat_row = db.execute(
                text(
                    "SELECT id FROM catalogs WHERE owner_workspace_id = :ws_id AND deleted_at IS NULL ORDER BY is_default DESC, created_at ASC LIMIT 1"
                ),
                {"ws_id": workspace_id},
            ).first()

            if not cat_row:
                # Create a default catalog if none exists
                cat_id = str(uuid.uuid4())
                db.execute(
                    text(
                        "INSERT INTO catalogs (id, owner_workspace_id, name, slug, is_default, created_at) "
                        "VALUES (:id, :ws_id, 'Default Catalog', 'default', true, :now)"
                    ),
                    {"id": cat_id, "ws_id": workspace_id, "now": datetime.now(UTC)},
                )
                db.commit()
            else:
                cat_id = str(cat_row[0])

            # 3. Resolve or find default schema
            schema_row = db.execute(
                text(
                    "SELECT id FROM catalog_schemas WHERE catalog_id = :cat_id AND slug IN ('default', 'bronze') LIMIT 1"
                ),
                {"cat_id": cat_id},
            ).first()

            if not schema_row:
                schema_id = str(uuid.uuid4())
                db.execute(
                    text(
                        "INSERT INTO catalog_schemas (id, catalog_id, name, slug, tier, created_at) "
                        "VALUES (:id, :cat_id, 'default', 'default', 'bronze', :now)"
                    ),
                    {"id": schema_id, "cat_id": cat_id, "now": datetime.now(UTC)},
                )
                db.commit()
            else:
                schema_id = str(schema_row[0])

            # 4. Resolve or upsert webhook_events table
            s3_path_pattern = f"s3://{MINIO_BUCKET}/workspaces/{ws_slug}/raw/webhook/**/*.parquet"
            table_row = db.execute(
                text(
                    "SELECT id FROM catalog_tables WHERE schema_id = :schema_id AND slug = 'webhook_events'"
                ),
                {"schema_id": schema_id},
            ).first()

            cols_json = json.dumps(WEBHOOK_COLUMNS)

            if not table_row:
                tbl_id = str(uuid.uuid4())
                db.execute(
                    text(
                        "INSERT INTO catalog_tables (id, schema_id, name, slug, description, s3_path_pattern, column_defs, schema_drift, created_at) "
                        "VALUES (:id, :schema_id, 'Webhook Events', 'webhook_events', 'Generic REST webhook landing events', :path, CAST(:cols AS jsonb), false, :now)"
                    ),
                    {
                        "id": tbl_id,
                        "schema_id": schema_id,
                        "path": s3_path_pattern,
                        "cols": cols_json,
                        "now": datetime.now(UTC),
                    },
                )
                logger.info(
                    "Registered new catalog table default.default.webhook_events for workspace '%s'",
                    ws_slug,
                )
            else:
                tbl_id = str(table_row[0])
                db.execute(
                    text(
                        "UPDATE catalog_tables SET s3_path_pattern = :path, column_defs = CAST(:cols AS jsonb) WHERE id = :id"
                    ),
                    {"id": tbl_id, "path": s3_path_pattern, "cols": cols_json},
                )
                logger.info(
                    "Updated existing catalog table webhook_events for workspace '%s'", ws_slug
                )

            db.commit()
    finally:
        db.close()


with DAG(
    dag_id="ingest_webhook",
    default_args=default_args,
    description="Micro-batch compaction and catalog registration for generic REST webhooks",
    schedule="*/15 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    is_paused_upon_creation=False,
    tags=["ingest", "webhook"],
    doc_md="Compacts raw webhook buffer files into date-partitioned Parquet files and auto-registers the webhook_events catalog table.",
) as dag:
    compact_task = PythonOperator(
        task_id="compact_webhook_buffers",
        python_callable=compact_webhook_buffers,
    )

    register_task = PythonOperator(
        task_id="register_catalog_tables",
        python_callable=register_catalog_tables,
    )

    compact_task >> register_task
