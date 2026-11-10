FROM apache/airflow:2.10.3-python3.11
RUN pip install --no-cache-dir \
    apache-airflow-providers-cncf-kubernetes \
    apache-airflow-providers-amazon \
    duckdb \
    boto3 \
    pyarrow \
    google-genai \
    scikit-learn \
    papermill \
    ipykernel

USER airflow
