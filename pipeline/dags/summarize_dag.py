from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="summarize",
    schedule="0 7 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:default", "ai"],
    doc_md="Generate daily Gemini summaries from yesterday's raw data into s3://datalake/raw/",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/summarize.py",
    )
