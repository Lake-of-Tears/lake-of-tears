from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator

logger = logging.getLogger("airflow.task.run_notebook")

default_args = {
    "owner": "airflow",
    "retries": 1,
    "retry_delay": timedelta(seconds=10),
}

JUPYTER_SERVER_URL = os.getenv("JUPYTER_SERVER_URL", "http://lake-of-tears-jupyter:8888").rstrip(
    "/"
)
JUPYTER_BASE_URL = os.getenv("JUPYTER_BASE_URL", "/jupyter").strip("/")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")


def _get_jupyter_api_base() -> str:
    if JUPYTER_BASE_URL:
        return f"{JUPYTER_SERVER_URL}/{JUPYTER_BASE_URL}"
    return JUPYTER_SERVER_URL


def _fetch_source_notebook(workspace_slug: str, notebook_path: str) -> dict[str, Any]:
    """Fetch the source notebook content from Jupyter Server REST API."""
    import requests

    clean_slug = workspace_slug.replace("\\", "/").strip("/ ") or "default"
    clean_nb = notebook_path.replace("\\", "/").strip("/ ")
    url = f"{_get_jupyter_api_base()}/api/contents/workspaces/{clean_slug}/{clean_nb}"
    headers = {"Content-Type": "application/json"}
    if JUPYTER_TOKEN:
        headers["Authorization"] = f"token {JUPYTER_TOKEN}"

    resp = requests.get(url, headers=headers, params={"type": "notebook"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("content", data)


def _upload_artifact_to_s3(
    workspace_slug: str, notebook_path: str, executed_path: str, execution_ts: str
) -> str:
    """Upload executed notebook artifact to MinIO S3."""
    import boto3

    clean_slug = workspace_slug.replace("\\", "/").strip("/ ") or "default"
    clean_name = notebook_path.replace("\\", "/").strip("/ ").replace(".ipynb", "")
    s3_key = f"notebook_runs/{clean_slug}/{clean_name}/{execution_ts}.ipynb"

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{MINIO_ENDPOINT}",
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )
    with open(executed_path, "rb") as f:
        s3.upload_fileobj(f, MINIO_BUCKET, s3_key)

    logger.info("Successfully archived notebook run to s3://%s/%s", MINIO_BUCKET, s3_key)
    return s3_key


def execute_notebook_task(**context) -> dict[str, Any]:
    """Execute notebook headlessly using Papermill and archive the resulting artifact."""
    import papermill as pm

    dag_run = context.get("dag_run")
    conf = dag_run.conf if dag_run and dag_run.conf else {}

    workspace_slug = (conf.get("workspace_slug") or conf.get("workspace") or "default").strip(
        "/"
    ) or "default"
    notebook_path = conf.get("notebook_path", "analysis.ipynb")
    parameters = conf.get("parameters", {})
    triggered_by = conf.get("triggered_by", "system")

    execution_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
    logger.info(
        "Starting headless notebook run for %s/%s triggered by %s",
        workspace_slug,
        notebook_path,
        triggered_by,
    )

    # Inject workspace execution context parameters and environment variables
    merged_parameters = {
        "WORKSPACE_SLUG": workspace_slug,
        "WORKSPACE": workspace_slug,
        "NOTEBOOK_PATH": notebook_path,
        "MINIO_ENDPOINT": MINIO_ENDPOINT,
        "MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
        "MINIO_SECRET_KEY": MINIO_SECRET_KEY,
        "MINIO_BUCKET": MINIO_BUCKET,
    }
    merged_parameters.update(parameters)

    os.environ["WORKSPACE_SLUG"] = workspace_slug
    os.environ["WORKSPACE"] = workspace_slug

    # 1. Fetch source notebook
    nb_dict = _fetch_source_notebook(workspace_slug, notebook_path)

    # 2. Run Papermill
    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = os.path.join(tmpdir, "input.ipynb")
        output_file = os.path.join(tmpdir, "output.ipynb")

        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(nb_dict, f)

        pm.execute_notebook(
            input_path=input_file,
            output_path=output_file,
            parameters=merged_parameters,
            log_output=True,
        )

        # 3. Archive output to MinIO S3
        s3_key = _upload_artifact_to_s3(workspace_slug, notebook_path, output_file, execution_ts)

    return {
        "status": "success",
        "workspace_slug": workspace_slug,
        "notebook_path": notebook_path,
        "s3_key": s3_key,
        "execution_ts": execution_ts,
    }


with DAG(
    dag_id="run_notebook",
    schedule=None,  # Triggered on-demand via Lake of Tears API
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["notebooks", "papermill"],
    doc_md="Headless parameterized execution of Lake of Tears notebooks via Papermill.",
    default_args=default_args,
) as dag:
    run_task = PythonOperator(
        task_id="execute_and_archive_notebook",
        python_callable=execute_notebook_task,
    )
