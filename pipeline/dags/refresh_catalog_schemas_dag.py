from __future__ import annotations

import os
from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator

default_args = {"owner": "airflow", "retries": 1}

BACKEND_URL = os.getenv("BACKEND_URL", "http://lake-of-tears-backend:8000")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")


def _duckdb_con():
    import duckdb

    con = duckdb.connect()
    con.execute(f"""
        INSTALL httpfs; LOAD httpfs;
        SET s3_endpoint='{MINIO_ENDPOINT}';
        SET s3_access_key_id='{MINIO_ACCESS_KEY}';
        SET s3_secret_access_key='{MINIO_SECRET_KEY}';
        SET s3_use_ssl=false;
        SET s3_url_style='path';
    """)
    return con


def _infer_columns(s3_path_pattern: str) -> list[dict] | None:
    try:
        con = _duckdb_con()
        rel = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{s3_path_pattern}') LIMIT 0")
        rows = rel.fetchall()
        con.close()
        return [
            {"name": r[0], "type": r[1], "description": None, "deprecated": False} for r in rows
        ]
    except Exception:
        return None


def _merge_columns(existing: list[dict], inferred: list[dict]) -> tuple[list[dict], bool]:
    existing_map = {c["name"]: c for c in (existing or [])}
    inferred_names = {c["name"] for c in inferred}
    merged = []
    drift = False

    for col in inferred:
        if col["name"] in existing_map:
            entry = dict(existing_map[col["name"]])
            if entry.get("deprecated"):
                entry["deprecated"] = False
                drift = True
            merged.append(entry)
        else:
            merged.append(col)
            drift = True

    for name, col in existing_map.items():
        if name not in inferred_names and not col.get("deprecated"):
            merged.append({**col, "deprecated": True})
            drift = True

    return merged, drift


def refresh_catalog_schemas():
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
    db = Session()

    try:
        # Import models inline to avoid circular issues in Airflow worker
        from sqlalchemy import text

        tables = db.execute(
            text(
                "SELECT id, s3_path_pattern, column_defs FROM catalog_tables WHERE s3_path_pattern IS NOT NULL"
            )
        ).fetchall()

        for row in tables:
            table_id, path, existing_cols = row
            inferred = _infer_columns(path)
            if inferred is None:
                continue

            merged, drift = _merge_columns(existing_cols or [], inferred)
            db.execute(
                text(
                    "UPDATE catalog_tables SET column_defs = CAST(:cols AS jsonb), schema_drift = :drift WHERE id = :id"
                ),
                {"cols": __import__("json").dumps(merged), "drift": drift, "id": str(table_id)},
            )

        db.commit()
        print(f"Refreshed {len(tables)} catalog tables")
    finally:
        db.close()


with DAG(
    dag_id="refresh_catalog_schemas",
    default_args=default_args,
    description="Nightly schema inference and drift detection for catalog tables",
    schedule_interval="0 6 * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    is_paused_upon_creation=False,
    tags=["catalog"],
) as dag:
    PythonOperator(
        task_id="refresh_catalog_schemas",
        python_callable=refresh_catalog_schemas,
    )
