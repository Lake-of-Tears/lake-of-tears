from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="anomaly_detect",
    schedule="0 8 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["ai"],
    doc_md="Run Isolation Forest anomaly detection on TrueNAS metrics; narrate findings with gemini-3.8-flash",
) as dag:
    BashOperator(
        task_id="run",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/anomaly_detect.py",
    )
