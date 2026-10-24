from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="ingest_truenas_logs",
    schedule="0 0 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["ingestion", "workspace:default"],
    params={"workspace": "default"},
    doc_md="Pull yesterday's TrueNAS journal logs (WARNING+) and active alerts into s3://datalake/workspaces/<workspace>/raw/truenas_logs/",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/ingest_truenas_logs.py",
        env={"WORKSPACE_SLUG": "{{ dag_run.conf.get('workspace', params.workspace) }}"},
    )
