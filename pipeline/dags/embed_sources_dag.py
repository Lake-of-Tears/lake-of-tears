from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="embed_sources",
    schedule="30 6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:default", "ai", "embeddings"],
    doc_md="Generate Gemini 768-dim embeddings for yesterday's raw data into s3://datalake/embeddings/",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/embed_sources.py",
    )
