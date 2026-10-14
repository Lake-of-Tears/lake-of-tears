# Technical Specification: Native Lake of Tears Airflow UI & Pipeline Orchestration

**Issue Link:** [#91 - feat(pipelines): native Lake of Tears Airflow UI and pipeline orchestration](https://github.com/Lake-of-Tears/lake-of-tears/issues/91)  
**Status:** In Review  
**Milestone:** `version 1.0.0`  
**Target Release:** Headless Core Backend (`backend/`), Native Frontend UI (`ui/`), Notebook Scheduler  

---

## 1. Executive Summary

Historically, Lake of Tears embedded Apache Airflow via an `<iframe>` pointing to `/airflow/` inside `ui/templates/embed.html`. This created significant product limitations:
1. **Broken Theme Consistency:** Airflow's default white/grey theme clashed with Lake of Tears' dark slate theme and failed to toggle when users switched between dark and light modes.
2. **Poor Security & Tenancy:** Users inside the iframe were exposed to all global Airflow DAGs without workspace scoping or RBAC enforcement matching Lake of Tears workspace roles (`admin`, `editor`, `viewer`).
3. **Incompatible with Headless Automation:** The upcoming Lake of Tears CLI (`lake`) requires headless Bearer token access to orchestrate pipelines, which an iframe cannot provide.
4. **Disjointed Notebook Scheduling:** Notebooks in `/notebooks` lacked first-class recurring schedule management directly linked to Airflow pipelines.

This specification details the transition to a **native Lake of Tears UI for pipelines**, powered by a centralized gateway in **Core Backend Router (`backend/main.py`)** that proxies and controls Airflow through its REST API, enforces workspace tenancy and RBAC, creates dynamic isolated DAGs (Databricks Job Cluster Model), and supports full dual theme switching (`data-theme="dark"` / `data-theme="light"`).

---

## 2. High-Level Architecture

```
┌─────────────────────────────────┐         ┌─────────────────────────────────┐
│        Lake of Tears UI         │         │        Lake of Tears CLI        │
│  (ui/templates/pipelines.html)  │         │    (lake pipeline run/status)   │
└────────────────┬────────────────┘         └────────────────┬────────────────┘
                 │                                           │
                 │ REST (Cookie or Bearer)                   │ REST (Bearer Token)
                 ▼                                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Lake of Tears Backend (backend/main.py)                  │
│                                                                             │
│  /api/v1/pipelines                                                         │
│    - GET    /                               (Workspace DAGs & statuses)     │
│    - GET    /{dag_id}                       (Single DAG metadata)           │
│    - PATCH  /{dag_id}                       (Pause / Unpause toggle)        │
│    - POST   /{dag_id}/trigger               (Trigger on-demand DAG run)     │
│    - GET    /{dag_id}/runs                  (Execution run history)         │
│    - GET    /{dag_id}/runs/{run_id}         (Run details)                   │
│    - GET    /{dag_id}/runs/{run_id}/tasks   (Task instance breakdown)       │
│    - GET    /{dag_id}/runs/{run_id}/tasks/  (Task execution logs)           │
│             {task_id}/logs                                                  │
│    - POST   /schedule-notebook              (Dynamic workspace DAG create)  │
│    - DELETE /schedule-notebook/{dag_id}     (Dynamic workspace DAG delete)  │
│                                                                             │
│  RBAC Guard:                                                                │
│    - viewer: Read-only inspection of DAGs, runs, and logs (403 on mutation) │
│    - editor / admin: Trigger runs, pause/resume, and schedule notebooks     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                        Airflow REST API (/api/v1/...)
                        & Dynamic DAG File Management
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Apache Airflow Execution Engine                       │
│                                                                             │
│  - Workspace DAG storage: /opt/airflow/dags/workspaces/{workspace_slug}/    │
│  - Tags: ["workspace:{workspace_slug}", "notebooks"]                        │
│  - Isolated Compute Execution (Databricks Job Cluster Model):               │
│      • Kubernetes: KubernetesPodOperator with CPU/RAM requests & limits     │
│      • Docker/Local: Constrained container/worker with execution timeouts   │
│  - Artifact Archiving: MinIO S3                                             │
│      s3://datalake/notebook_runs/{workspace_slug}/{notebook}/{timestamp}.ipynb│
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Backend Implementation Specification

### 3.1. Airflow Service Layer (`backend/pipeline_service.py`)
Encapsulates communication with Airflow REST API (`/api/v1/...`) and dynamic DAG generation:
- **Authentication:** HTTP Basic Auth with `AIRFLOW_ADMIN_USER` and `AIRFLOW_ADMIN_PASSWORD`.
- **Base URL Resolution:** `AIRFLOW_URL` (default: `http://airflow:8080/airflow` or `http://airflow:8080`).
- **Workspace Filtering:**
  - Airflow API `GET /api/v1/dags?tags={workspace_slug}` or post-filtering by tag `workspace:{workspace_slug}`.
  - DAGs without workspace tags are either classified as system/global or hidden from tenant views based on admin settings.
- **Dynamic DAG Generation:**
  - Workspace directory: `${AIRFLOW_DAGS_DIR}/workspaces/{workspace_slug}/`.
  - Generates Python DAG code for recurring scheduled notebook jobs.
  - Compute template: `KubernetesPodOperator` on K8s clusters (falling back to parameterized `Papermill` worker task on Docker).
  - Configures CPU (e.g. `1 core`), Memory (e.g. `2Gi`), and execution timeout (`2 hours`).
  - Automatically tags DAG with `workspace:{workspace_slug}` and notebook metadata.

### 3.2. Pipelines API Router (`backend/pipelines_router.py`)
Mounted at `/api/v1/pipelines` and `/api/pipelines`:

| Method | Endpoint | Description | Min Role Required |
|---|---|---|---|
| `GET` | `/` | List workspace DAGs with pause status, last run, next run | `viewer` |
| `GET` | `/{dag_id}` | Detailed DAG metadata | `viewer` |
| `PATCH` | `/{dag_id}` | Pause / unpause DAG (`{"is_paused": bool}`) | `editor` |
| `POST` | `/{dag_id}/trigger` | Trigger manual run with parameters (`{"conf": dict}`) | `editor` |
| `GET` | `/{dag_id}/runs` | List execution history for a DAG | `viewer` |
| `GET` | `/{dag_id}/runs/{run_id}` | Specific DAG run status and timings | `viewer` |
| `GET` | `/{dag_id}/runs/{run_id}/tasks` | Task instances for a DAG run | `viewer` |
| `GET` | `/{dag_id}/runs/{run_id}/tasks/{task_id}/logs` | Fetch task execution log (with `try_number`) | `viewer` |
| `POST` | `/schedule-notebook` | Create/update recurring notebook schedule DAG | `editor` |
| `DELETE` | `/schedule-notebook/{dag_id}` | Delete scheduled notebook DAG | `editor` |

### 3.3. RBAC Enforcement
- Evaluated via `get_notebook_workspace_and_role` or pipeline-specific dependency in `backend/auth.py`.
- **`viewer`:** Read-only access to `/` (list), `/{dag_id}`, `/runs`, `/tasks`, and `/logs`. Attempting to trigger, pause, or create/delete schedules yields `403 Forbidden` (`{"detail": "Role viewer cannot mutate or trigger pipelines"}`).
- **`editor` / `admin` / `superadmin`:** Full permission to trigger runs, modify pause states, and create/delete schedules.

---

## 4. Frontend UI Specification (`ui/templates/pipelines.html`)

### 4.1. Layout & Styling
- **Replaces `embed.html` iframe** at route `/pipelines`.
- **Dual-Theme Compliant:**
  - Dark Theme (`data-theme="dark"`): Background `#111111`, Card `#161616`, Borders `rgba(255,255,255,0.07)`, Text `#f3f4f6`, Accent `#3b82f6`.
  - Light Theme (`data-theme="light"`): Background `#f5f5f5`, Card `#ffffff`, Borders `#e5e7eb`, Text `#111111`, Accent `#2563eb`.
- **Header:**
  - Title: "Pipelines" with active workspace indicator pill.
  - Search input: Live fuzzy filtering by pipeline name or tags.
  - Status filter pills: All, Active, Paused, Failed.
  - Action buttons: "Refresh" (with last updated timestamp) and "New Schedule" modal launcher.

### 4.2. Pipeline Cards & Table View
- **Status Toggle:** Inline toggle switch to Pause/Unpause DAG with immediate UI response.
- **Pipeline Metadata:** Name, description, schedule interval badge (e.g. `@daily`, `@hourly`, `0 2 * * *`), and tags.
- **Last Run Pill:** Visual status badge (Success: green, Running: animated blue pulse, Failed: red, Queued: amber).
- **Run Duration & Next Run Time:** Formatted in human-readable local/UTC time.
- **Action Controls:**
  - **"Run Now" (Play Icon):** Triggers run immediately or opens parameter injection modal.
  - **"History / Logs" (Terminal/List Icon):** Opens Slide-Over Inspector Drawer.

### 4.3. Run History & Task Log Inspector Drawer
- Slide-over drawer opening from the right side of the screen.
- **Runs Tab:** Chronological list of historical runs with run IDs, trigger type (`manual` vs `scheduled`), start/finish times, and duration.
- **Tasks Breakdown:** Shows each task in the DAG run (e.g., `execute_and_archive_notebook`), state, and retry count.
- **Interactive Log Viewer:**
  - Renders raw logs with ANSI color escape code parsing (`ansi_up`).
  - Terminal-like dark styling with monospace font, line numbers, and "Scroll to bottom" button.
  - Auto-polling while task state is `running`.

---

## 5. Notebook Orchestration Integration (`ui/templates/notebooks.html`)

### 5.1. Enhanced "Schedule / Run" Modal
The existing notebook "Schedule / Run" modal is expanded with two distinct tabs:
1. **Immediate Execution ("Run Now"):**
   - Injects JSON parameters and immediately triggers `run_notebook` DAG via `POST /api/v1/pipelines/run_notebook/trigger`.
   - Links directly to live execution in the Pipelines viewer.
2. **Recurring Schedule ("Recurring Pipeline"):**
   - **Preset Cadence:** Hourly (`@hourly`), Daily at midnight (`@daily`), Weekly (`@weekly`), or Custom Cron expression (`0 4 * * 1-5`).
   - **Parameter Defaults:** Key-value or JSON config passed to Papermill on each scheduled invocation.
   - **Pipeline Title:** Automatically generated or customized (e.g., `ws-analytics-daily-etl`).
   - Calls `POST /api/v1/pipelines/schedule-notebook`.
   - Persists dynamic DAG file into workspace DAG directory.

---

## 6. Compute Isolation Model (Databricks Job Cluster Model)

To prevent noisy-neighbor starvation where long-running notebooks consume CPU/RAM required by production ingestion pipelines:
1. **Kubernetes Environment:**
   - Dynamic DAGs invoke `KubernetesPodOperator`.
   - Each run launches an isolated ephemeral worker pod in the `lake-of-tears` namespace.
   - Resource quotas applied: `requests: {cpu: "500m", memory: "1Gi"}`, `limits: {cpu: "2", memory: "4Gi"}`.
   - Pod is deleted immediately after execution completes (`is_delete_operator_pod=True`).
2. **Local / Docker Compose Environment:**
   - Tasks execute with explicit execution timeouts (`execution_timeout=timedelta(hours=2)`).
   - Temporary scratch directories isolated per execution run in `/tmp/lake_runs/{run_id}`.
3. **Artifact Archival:**
   - Executed notebook and HTML report are written directly to MinIO S3:
     `s3://datalake/notebook_runs/{workspace_slug}/{notebook_name}/{execution_date}.ipynb`
   - Metadata is registered in backend for immediate preview in Notebooks Run History.

---

## 7. Verification & Testing Strategy

### 7.1. Automated Unit & Integration Tests (`tests/test_backend_pipelines.py`)
- **Workspace Tenancy & Tag Filtering:** Verify `GET /api/v1/pipelines` only returns DAGs matching the active workspace.
- **RBAC Role Matrix:**
  - `viewer` can view DAGs, runs, task instances, and logs.
  - `viewer` receives `403 Forbidden` when attempting to trigger, pause, or schedule DAGs.
  - `editor` and `admin` successfully trigger and mutate pipelines.
- **Dynamic DAG Creation:** Verify `POST /api/v1/pipelines/schedule-notebook` generates valid Python Airflow DAG code with isolated task operators, correct schedule interval, and workspace tags.
- **Log Streaming:** Verify retrieval and sanitization of task instance execution logs.
- **Bearer Token Auth:** Verify CLI can authenticate headlessly via `Authorization: Bearer <lake_token>`.

### 7.2. End-to-End & UI Verification
- Verify `/pipelines` renders native HTML without `<iframe>`.
- Toggle between light and dark modes and verify all text, cards, borders, badges, and terminal logs remain legible and aesthetically aligned.
- Trigger a notebook schedule from `/notebooks` and confirm it appears in the `/pipelines` table with correct metadata.
- Run `uv run pytest tests/` across all 70+ test cases to verify 100% pass rate.
- Run `uv run ruff check` and `uv run ruff format --check` to guarantee code cleanliness.
