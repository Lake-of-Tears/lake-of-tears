from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="ingest_truenas",
    schedule="0 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:default", "ingestion"],
    doc_md="Ingest TrueNAS pool stats and SMART metrics into s3://datalake/raw/truenas/",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/ingest_truenas.py",
    )
