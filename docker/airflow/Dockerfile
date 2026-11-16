FROM apache/airflow:2.10.3-python3.11
RUN pip install --no-cache-dir \
    duckdb \
    boto3 \
    "pyarrow==25.0.1" \
    "numpy<2" \
    "pyiceberg[pyarrow]==0.12.0" \
    google-genai \
    scikit-learn \
    papermill \
    ipykernel

# Fail the build if the worker image cannot import the Iceberg write path used by
# bundle duckdb_sql tasks (pyarrow 26+ needs NumPy 2, Airflow 2.10.3 pins NumPy 1.26).
RUN python -c "import numpy, pyarrow, duckdb, pyiceberg; from pyiceberg.io.pyarrow import PyArrowFileIO; duckdb.sql('select 1').to_arrow_table(); print('numpy', numpy.__version__, 'pyarrow', pyarrow.__version__, 'pyiceberg', pyiceberg.__version__)"

USER airflow
