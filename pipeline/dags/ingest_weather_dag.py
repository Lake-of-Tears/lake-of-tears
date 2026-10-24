from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="ingest_weather",
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["ingestion", "workspace:default"],
    params={"workspace": "default"},
    doc_md="Ingest hourly Open-Meteo weather forecast into s3://datalake/workspaces/<workspace>/raw/weather/",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/ingest_weather.py",
        env={"WORKSPACE_SLUG": "{{ dag_run.conf.get('workspace', params.workspace) }}"},
    )
