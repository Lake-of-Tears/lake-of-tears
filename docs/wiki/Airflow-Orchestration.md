# Airflow Orchestration and Scheduling

Apache Airflow runs everything that happens on a schedule in Lake of Tears: ingestion, embeddings, AI summaries, catalog upkeep and notebook runs. This guide explains how Airflow is set up in the stack, the standards the shipped DAGs follow (with a template you can copy), and how to run, inspect and troubleshoot DAGs.

Commands in this guide were run against a Docker Compose stack on Airflow 2.10.3. Use `docker compose exec airflow airflow ...` for the CLI examples.

## 1. How Airflow is set up

| | Docker Compose | Kubernetes / Helm |
|---|---|---|
| Image | `apache/airflow:2.10.3-python3.11`, with `duckdb`, `requests`, `boto3`, `google-genai` and `scikit-learn` installed at start | Image built from `docker/airflow/Dockerfile`: the same base plus `duckdb`, `boto3`, `pyarrow`, `google-genai`, `scikit-learn`, `papermill` and `ipykernel` |
| Run mode | `airflow standalone`: web server, scheduler and triggerer in one container | One deployment, with `AIRFLOW__CORE__EXECUTOR=LocalExecutor` |
| Executor | `SequentialExecutor`: **one task at a time** | `LocalExecutor`: tasks run in parallel on the pod |
| Metadata database | SQLite file `airflow.db` | PostgreSQL |
| Web UI | `http://localhost/airflow/` (through nginx) | `https://<ingress.host>/airflow` (Ingress) |

### Folders and container paths

In Docker Compose two host folders under `DATALAKE_DATA_DIR` (set in `.env`) are mounted into the container:

| Host folder | Inside the container | What lives there |
|---|---|---|
| `$DATALAKE_DATA_DIR/airflow/` | `/opt/airflow/` | `dags/` (DAG files), `logs/`, `airflow.db`, `airflow.cfg` |
| `$DATALAKE_DATA_DIR/datalake/` | `/opt/airflow/datalake/` | pipeline code: `storage_writer.py` and `jobs/pipeline/*.py` |

The backend container mounts the same `dags/` folder (`AIRFLOW_DAGS_DIR=/opt/airflow/dags`), which is how it writes DAG files for notebooks scheduled from the Lake UI.

```
$DATALAKE_DATA_DIR/
├── airflow/
│   ├── dags/                    <- DAG files (shipped DAGs and generated workspace DAGs)
│   ├── logs/dag_id=.../run_id=.../task_id=.../attempt=N.log
│   └── airflow.db
└── datalake/
    ├── storage_writer.py
    └── jobs/pipeline/           <- scripts run by BashOperator DAGs
```

The repository does not copy DAGs or scripts into these folders for you. Copy a DAG file into `dags/` yourself; see [Data Ingestion and Pipelines](Data-Ingestion-and-Pipelines.md) for the script layout.

### Web UI and routing

nginx proxies `/airflow/` to Airflow. Airflow is configured with a base URL that includes the `/airflow` path (`AIRFLOW__WEBSERVER__BASE_URL`), and nginx must pass the prefix through unchanged. Two details matter:

- The base URL **must not end with a slash**, or the web server refuses to start with `webserver.base_url conf cannot have a trailing slash`.
- Open `/airflow/` (with the prefix), not Airflow's direct port.

If `http://localhost/airflow/` returns 502, check `docker compose logs airflow` for that message. See [#163](https://github.com/Lake-of-Tears/lake-of-tears/issues/163).

### Credentials

The Lake UI's pipeline pages call Airflow's REST API with HTTP basic auth (`AIRFLOW_URL`, `AIRFLOW_ADMIN_USER`, `AIRFLOW_ADMIN_PASSWORD`). In Docker Compose, `airflow standalone` creates its own `admin` user and writes the generated password to `$DATALAKE_DATA_DIR/airflow/standalone_admin_password.txt`; it ignores the `AIRFLOW_PASSWORD` value in `.env`. Use that file's contents to sign in to the web UI, and see [#165](https://github.com/Lake-of-Tears/lake-of-tears/issues/165) for the REST-API mismatch.

## 2. The DAGs that ship today

| DAG | File | Schedule | Operator | Purpose |
|---|---|---|---|---|
| `embed_sources` | `embed_sources_dag.py` | daily 06:30 | Bash | Vector embeddings into `embeddings/` |
| `summarize` | `summarize_dag.py` | daily 07:00 | Bash | AI daily summaries |
| `anomaly_detect` | `anomaly_detect_dag.py` | daily 08:00 | Bash | Isolation Forest anomaly detection |
| `refresh_catalog_schemas` | `refresh_catalog_schemas_dag.py` | daily 06:00 | Python | Schema drift detection for catalog tables |
| `purge_deleted_catalogs` | `purge_deleted_catalogs_dag.py` | daily 02:00 | Python | Permanently remove expired soft-deleted catalogs |
| `purge_inactive_workspaces` | `purge_inactive_workspaces_dag.py` | hourly | Python | Permanently remove expired inactive workspaces |
| `run_notebook` | `run_notebook_dag.py` | none (on demand) | Python | Run a notebook when triggered through the Lake of Tears API |

Notebooks that users schedule from the Lake UI become extra DAGs: the backend generates one DAG file per schedule into the `dags/` folder, using a `PythonOperator` by default or a `KubernetesPodOperator` when `EXECUTION_MODE=k8s`.

The catalog and workspace maintenance DAGs are described in [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md).

## 3. DAG creation standard

### Naming

| Kind | `dag_id` | File | Tags |
|---|---|---|---|
| Ingestion | `ingest_<source>` | `ingest_<source>_dag.py` | `ingestion` |
| Embeddings and AI | `embed_sources`, `summarize`, `anomaly_detect` | `<dag_id>_dag.py` | `ai`, `embeddings` |
| Catalog and workspace upkeep | `purge_*`, `refresh_*` | `<dag_id>_dag.py` | `catalog`, `workspace`, `maintenance` |

Keep the `dag_id` equal to the file name without `_dag.py`, so it is easy to find.

### Which workspace sees a DAG

The Pipelines page of a workspace lists a DAG when any of these is true:

| Rule | Example for the workspace `default` |
|---|---|
| The `dag_id` starts with `ws_<slug>_` | `ws_default_daily_report` |
| The DAG has the tag `workspace:<slug>` | `tags=["ingestion", "workspace:default"]` |

A DAG that matches none of these (for example untagged maintenance DAGs) is not listed.

The global on-demand runner DAG (`run_notebook`) is strictly an internal headless executor and is excluded from workspace DAG listings. Furthermore, execution runs and logs for `run_notebook` are strictly scoped to the workspace in which the notebook was triggered.

Superadmins are strictly jailed to the active workspace context (enforced in `is_dag_accessible_by_workspace` in `backend/pipeline_service.py`), preventing cross-workspace leakage of DAGs, runs, or execution logs.

### Schedules, catchup and retries

- **Fixed `start_date`** in the past (the shipped DAGs use `datetime(2026, 1, 1)`) with **`catchup=False`**. Airflow does not create runs for every missed interval; backfill on purpose with `airflow dags backfill` (see the ingestion guide).
- **Use the `schedule` argument** with a cron string. A few older DAGs use the deprecated `schedule_interval`; prefer `schedule` in new code.
- **Set retries explicitly.** The ingest DAGs shipped today set none, so a failed run is not retried. The maintenance DAGs set `retries: 3` with a 5-minute delay. For new DAGs use something like `retries: 2` and `retry_delay: 5 minutes`.
- Set `schedule=None` for DAGs that are only ever triggered by hand or by an API call (`run_notebook`).
- Put a one-line `doc_md` on every DAG; it shows in the Airflow UI.

### BashOperator or PythonOperator

| Use | When | Why |
|---|---|---|
| **BashOperator** running a script | Ingestion and AI jobs | The script is a normal program you can also run by hand and test outside Airflow. It runs in its own process and reads secrets from the environment. |
| **PythonOperator** | Maintenance jobs that need the database, MinIO or the backend, and workspace notebook runs | The logic lives in the DAG file. Import heavy libraries **inside** the callable so that parsing the DAG file stays fast. |

### Standard DAG template

This template follows the standard above. It passes the run date to the script with `{{ ds }}`, and shows both operators.

```python
import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator


def summarize(ds, **_):
    # Context values such as `ds` are passed by name; read secrets from the environment.
    print(f"[my_dag] summarizing {ds}; bucket={os.environ.get('MINIO_BUCKET')}")


with DAG(
    dag_id="my_dag",
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["ingestion"],
    default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
    doc_md="One-line description shown in the Airflow UI.",
) as dag:
    extract = BashOperator(
        task_id="extract",
        bash_command="python3 /opt/airflow/datalake/jobs/pipeline/my_script.py {{ ds }}",
    )
    report = PythonOperator(task_id="report", python_callable=summarize)
    extract >> report
```

Save it as `my_dag_dag.py` in the `dags/` folder (`$DATALAKE_DATA_DIR/airflow/dags/` on the host).

New files are noticed within a few minutes (the scanner interval is 300 seconds). Force it with `airflow dags reserialize`. **New DAGs start paused**, so unpause them before they run on schedule.

## 4. Operations

### Find your DAGs

```bash
docker compose exec airflow airflow dags list
docker compose exec airflow airflow dags list-import-errors
```

`list-import-errors` prints the Python traceback for any DAG file that failed to load. A DAG with an import error simply does not appear in `dags list`, so check here first when a DAG is missing.

### Trigger a run

```bash
docker compose exec airflow airflow dags unpause my_dag
docker compose exec airflow airflow dags trigger my_dag
docker compose exec airflow airflow dags trigger my_dag -c '{"note": "manual"}'   # with run config
docker compose exec airflow airflow dags list-runs -d my_dag
```

You can also use the **Trigger DAG** button in the web UI. A manual run uses the current time as its logical date, so `{{ ds }}` is today.

### Test without the scheduler

```bash
docker compose exec airflow airflow dags test my_dag 2026-10-10          # runs the whole DAG in-process
docker compose exec airflow airflow tasks test my_dag extract 2026-10-10 # runs one task
```

Both print the task output straight to your terminal and do not need the DAG to be unpaused. This is the fastest way to debug a new DAG.

### Backfill

```bash
docker compose exec airflow airflow dags backfill -s 2026-10-10 -e 2026-10-12 my_dag
```

Each run gets its own logical date. This relies on your script taking the date as an argument; see [Data Ingestion and Pipelines](Data-Ingestion-and-Pipelines.md).

### Inspect task logs

Logs are files under `$DATALAKE_DATA_DIR/airflow/logs/`, one per attempt:

```
logs/dag_id=<dag>/run_id=<run>/task_id=<task>/attempt=1.log
```

In the web UI open the DAG, choose a run, click a task and open **Logs**. A task that was retried has one log per attempt. We saw this with a task that fails on its first attempt and succeeds on the retry: `attempt=1.log` held the failure and `attempt=2.log` the success, and the run ended in `success`.

Script output (`print`) appears in the task log, prefixed by Airflow's timestamp.

### Re-run something that failed

In the UI, select the failed task and choose **Clear**. From the CLI:

```bash
docker compose exec airflow airflow tasks clear my_dag -s 2026-10-10 -e 2026-10-10 -t extract -y
```

Clearing resets the task so the scheduler runs it again. Because ingestion writes are idempotent, re-running a day is safe.

### Pause and unpause

```bash
docker compose exec airflow airflow dags pause my_dag
docker compose exec airflow airflow dags unpause my_dag
```

### Remove a DAG

Delete the file from `dags/`, then run `airflow dags delete -y <dag_id>` to remove the DAG's metadata and run history from the database.

## 5. Credentials, Variables and Connections

The shipped DAGs do **not** use Airflow Variables or Connections. They read configuration from **environment variables** set on the `airflow` service in `docker-compose.yml` (and the Helm values), such as:

| Variable | Used for |
|---|---|
| `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET` | Reading and writing Parquet in MinIO |
| `BACKEND_URL`, `AUTH_SECRET_KEY` | Calling the Lake of Tears backend (for example to fetch the AI key configured in the Admin Panel) |
| `JUPYTER_SERVER_URL`, `JUPYTER_BASE_URL`, `JUPYTER_TOKEN` | Running notebooks through the Jupyter Server API |
| Source-specific keys (`TRUENAS_*`, `JELLYFIN_*`, `STRIPE_SECRET_KEY` and others) | Ingestion scripts |

To add a setting: put it in `.env` (and `.env.example`), pass it to the `airflow` service under `environment:`, then restart the service. The task reads it with `os.environ`.

Airflow's own **Variables** and **Connections** are supported if you prefer them:

```bash
docker compose exec airflow airflow variables set my_threshold 10
docker compose exec airflow airflow variables get my_threshold
docker compose exec airflow airflow connections add my_api --conn-type http \
  --conn-host api.example.com --conn-login user --conn-password '<secret>'
docker compose exec airflow airflow connections list
```

In a DAG you would read them with `Variable.get("my_threshold")` and `BaseHook.get_connection("my_api")`. Two cautions:

- The Compose stack sets **no Fernet key** (`AIRFLOW__CORE__FERNET_KEY` is empty), so Variables and connection passwords are not encrypted in the SQLite database. Set a key before storing real secrets this way, or keep using environment variables.
- Do not put secrets in `bash_command` or DAG file text, because those are shown in the web UI and logs.

## 6. Troubleshooting checklist

| Symptom | Check |
|---|---|
| `http://localhost/airflow/` returns 502 or 404 | `docker compose logs airflow`. A `trailing slash` error means the base URL ends in `/` (#163). A 404 on every page means nginx is stripping the `/airflow` prefix: the `proxy_pass` in the `/airflow/` location must have no URI after the port. |
| Cannot sign in, or the Lake UI cannot list DAGs | Compose generates the admin password; read `standalone_admin_password.txt` (#165). |
| A DAG file is not in the list | `airflow dags list-import-errors`. Then `airflow dags reserialize`. Confirm the file is in `$DATALAKE_DATA_DIR/airflow/dags/`. |
| DAG is listed but never runs | It may be **paused** (new DAGs are). `airflow dags unpause <dag_id>`. Also check the `schedule` and `start_date`. |
| Task fails immediately with `No such file or directory` | The script path. Scripts must be at `/opt/airflow/datalake/jobs/pipeline/`; check `$DATALAKE_DATA_DIR/datalake/jobs/pipeline/`. |
| `ModuleNotFoundError: storage_writer` | `storage_writer.py` must sit in `$DATALAKE_DATA_DIR/datalake/`, two folders above the script. |
| Task fails with a missing environment variable | The variable is not on the `airflow` service. Add it under `environment:` and restart Airflow. |
| A task waits a long time to start (Compose) | `SequentialExecutor` runs one task at a time; a long task delays the others. |
| Need to see why a run failed | Open the failed task's log (`attempt=N.log`), or reproduce with `airflow tasks test`. |
| Data is wrong for one day | Clear that day's task, or backfill that date, then check the partition in MinIO. |

## Related pages

- [Home](Home.md)
- [Architecture Overview](Architecture-Overview.md): where Airflow fits in the stack
- [Data Ingestion and Pipelines](Data-Ingestion-and-Pipelines.md): writing the scripts these DAGs run
- [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md): the maintenance DAGs
