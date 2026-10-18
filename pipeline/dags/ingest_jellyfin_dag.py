from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="ingest_jellyfin",
    schedule="0 */6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:default", "ingestion"],
    doc_md="Ingest Jellyfin play history into s3://datalake/raw/jellyfin/",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/ingest_jellyfin.py",
    )
