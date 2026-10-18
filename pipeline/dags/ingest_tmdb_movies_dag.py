from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator

logger = logging.getLogger("airflow.task.ingest_tmdb_movies")

default_args = {
    "owner": "airflow",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", os.getenv("MINIO_ROOT_USER", "admin"))
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", os.getenv("MINIO_ROOT_PASSWORD", "password123"))
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
JUPYTER_SERVER_URL = os.getenv("JUPYTER_SERVER_URL", "http://lake-of-tears-jupyter:8888").rstrip(
    "/"
)
JUPYTER_BASE_URL = os.getenv("JUPYTER_BASE_URL", "/jupyter").strip("/")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")


def _find_source_notebook() -> str:
    """Locate the tmdb_daily_movies_ingest.ipynb notebook across standard deployment paths or Jupyter REST API."""
    candidates = [
        Path("/opt/airflow/notebooks/tmdb_daily_movies_ingest.ipynb"),
        Path("/opt/airflow/dags/notebooks/tmdb_daily_movies_ingest.ipynb"),
        Path("/opt/airflow/datalake/notebooks/tmdb_daily_movies_ingest.ipynb"),
        Path(__file__).parent / "tmdb_daily_movies_ingest.ipynb",
        Path(__file__).parents[2] / "notebooks" / "tmdb_daily_movies_ingest.ipynb",
        Path("/home/jovyan/workspaces/default/tmdb_daily_movies_ingest.ipynb"),
        Path("/home/jovyan/work/notebooks/tmdb_daily_movies_ingest.ipynb"),
        Path("notebooks/tmdb_daily_movies_ingest.ipynb"),
    ]
    for p in candidates:
        if p.exists():
            return str(p.resolve())

    # Fall back to fetching from Jupyter Server REST API
    import requests

    api_base = (
        f"{JUPYTER_SERVER_URL}/{JUPYTER_BASE_URL}" if JUPYTER_BASE_URL else JUPYTER_SERVER_URL
    )
    url = f"{api_base}/api/contents/workspaces/default/tmdb_daily_movies_ingest.ipynb"
    headers = {"Authorization": f"token {JUPYTER_TOKEN}"} if JUPYTER_TOKEN else {}

    try:
        resp = requests.get(url, headers=headers, params={"type": "notebook"}, timeout=20)
        if resp.status_code == 200:
            content = resp.json().get("content")
            if content:
                cache_dir = Path("/opt/airflow/notebooks")
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached_path = cache_dir / "tmdb_daily_movies_ingest.ipynb"
                with open(cached_path, "w", encoding="utf-8") as f:
                    json.dump(content, f)
                logger.info(
                    "Successfully fetched and cached notebook from Jupyter Server at %s",
                    cached_path,
                )
                return str(cached_path)
    except Exception as exc:
        logger.warning("Could not retrieve notebook from Jupyter Server (%s): %s", url, exc)

    raise FileNotFoundError(
        f"Could not locate 'tmdb_daily_movies_ingest.ipynb'. Checked local filesystem and Jupyter Server at {url}."
    )


def _upload_run_artifact_to_s3(executed_path: str, execution_ts: str) -> str:
    """Upload executed notebook artifact to MinIO S3."""
    import boto3

    s3_key = f"notebook_runs/default/tmdb_daily_movies/{execution_ts}.ipynb"
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{MINIO_ENDPOINT}",
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )
    with open(executed_path, "rb") as f:
        s3.upload_fileobj(f, MINIO_BUCKET, s3_key)

    logger.info("Successfully archived TMDb notebook run to s3://%s/%s", MINIO_BUCKET, s3_key)
    return s3_key


def run_tmdb_notebook_pipeline(**context) -> dict[str, Any]:
    """Execute the TMDb ingestion notebook headlessly via Papermill."""
    import papermill as pm

    ds = context.get("ds", datetime.now(UTC).strftime("%Y-%m-%d"))
    execution_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")

    source_path = _find_source_notebook()
    logger.info("Executing TMDb notebook from %s for partition date %s", source_path, ds)

    dag_run = context.get("dag_run")
    conf = dag_run.conf if dag_run and dag_run.conf else {}
    tmdb_key = (
        conf.get("TMDB_API_KEY")
        or context.get("params", {}).get("TMDB_API_KEY")
        or os.getenv("TMDB_API_KEY", "")
    )
    if not tmdb_key:
        try:
            from airflow.models import Variable

            tmdb_key = Variable.get("TMDB_API_KEY", default_var="")
        except Exception:
            pass

    parameters = {
        "INGEST_DATE": str(ds),
        "MINIO_ENDPOINT": MINIO_ENDPOINT,
        "MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
        "MINIO_SECRET_KEY": MINIO_SECRET_KEY,
        "MINIO_BUCKET": MINIO_BUCKET,
    }
    if tmdb_key:
        parameters["TMDB_API_KEY"] = tmdb_key

    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = os.path.join(tmpdir, "output.ipynb")
        pm.execute_notebook(
            input_path=source_path,
            output_path=output_file,
            parameters=parameters,
            log_output=True,
        )
        s3_key = _upload_run_artifact_to_s3(output_file, execution_ts)

    return {
        "status": "success",
        "ingest_date": ds,
        "s3_key": s3_key,
        "execution_ts": execution_ts,
    }


with DAG(
    dag_id="ingest_tmdb_movies",
    schedule="0 2 * * *",  # Daily at 02:00 UTC
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["ingestion", "tmdb", "notebooks", "movies"],
    doc_md="Daily ingestion of trending and popular movies from TheMovieDB into s3://datalake/raw/tmdb/movies/ via Papermill.",
    default_args=default_args,
) as dag:
    PythonOperator(
        task_id="execute_tmdb_notebook",
        python_callable=run_tmdb_notebook_pipeline,
    )
