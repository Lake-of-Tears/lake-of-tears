# Contributing to Lake of Tears

Thank you for your interest in contributing to **Lake of Tears**!

Lake of Tears is the open-source, self-hosted Databricks alternative — a unified datalakehouse for Kubernetes and Docker. Community contributions make the platform faster, more reliable, and accessible for everyone.

---

## Community & Resources

- **GitHub Organization:** [github.com/Lake-of-Tears](https://github.com/Lake-of-Tears)
- **Main Repository:** [github.com/Lake-of-Tears/lake-of-tears](https://github.com/Lake-of-Tears/lake-of-tears)
- **Official Website:** [thelakeoftears.com](https://thelakeoftears.com)
- **Issues & Bug Reports:** [github.com/Lake-of-Tears/lake-of-tears/issues](https://github.com/Lake-of-Tears/lake-of-tears/issues)
- **Pull Requests:** [github.com/Lake-of-Tears/lake-of-tears/pulls](https://github.com/Lake-of-Tears/lake-of-tears/pulls)
- **Support & Security Inquiries:** [support@thelakeoftears.com](mailto:support@thelakeoftears.com)

---

## Ecosystem & Areas to Contribute

Lake of Tears brings analytical compute, storage, vector search, orchestration, and business intelligence together behind a single entry point. Contributions are welcome across all pillars:

1. 💧 **Lake UI Shell & Authentication** (`ui/`, `backend/`):
   - Databricks-inspired unified sidebar shell and responsive design.
   - FastAPI endpoints, session handling, and enterprise SSO (Google, GitHub, Microsoft Azure, generic OIDC).
2. 🦆 **DuckDB Analytical SQL Engine** (`pipeline/`, `ui/`):
   - Serverless in-process SQL execution against Hive-partitioned Parquet files (`year=/month=/day=`) on MinIO S3 without Spark cluster overhead.
3. 🤖 **AI & Vector Intelligence** (`pipeline/embed/`, `pipeline/query/`):
   - 768-dimensional embeddings generated with Google Gemini and stored natively in Parquet.
   - DuckDB VSS vector indexing (HNSW), RAG-grounded search, and Isolation Forest anomaly detection.
4. 🔄 **Airflow Pipelines & Data Connectors** (`pipeline/ingest/`, `pipeline/dags/`):
   - Turnkey data source connectors (e.g., Stripe, Shopify, HubSpot, PostgreSQL, Open-Meteo, system telemetry) with scheduled DAGs.
5. 📊 **Dashboards & Notebooks**:
   - Deep integration and iframe embedding for Apache Superset (BI dashboards) and JupyterLab (data science).
6. ☸️ **Cloud & Infrastructure** (`deploy/helm/`, `deploy/terraform/`, `docker/`):
   - Production Helm chart (`deploy/helm/lake-of-tears`) with Ingress routing and PVC storage backends (NFS, Longhorn, Ceph, local-path).
   - Docker Compose optimizations and Terraform infrastructure modules.
7. 📚 **Documentation & Guides** (`docs/`, `README.md`):
   - Architecture guides, deployment walkthroughs, example SQL analytics, and connector tutorials.

---

## Getting Started

### Prerequisites

- **Docker & Docker Compose** (v26+ recommended)
- **Python 3.12**
- **Helm 3.x** (if testing Kubernetes chart changes)
- **Terraform 1.9+** (if testing infrastructure changes)
- `git`

### Local Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Lake-of-Tears/lake-of-tears.git
   cd lake-of-tears
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` to configure your credentials (at minimum: `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `AUTH_SECRET_KEY`, and `GEMINI_API_KEY`).

3. **Start the stack:**
   ```bash
   docker compose up -d
   ```

4. **Access the services:**
   - **Unified Lake UI Shell:** [http://localhost](http://localhost) (or direct port at `http://localhost:3000`)
   - **Auth Backend:** `http://localhost/api/` (or direct port at `http://localhost:8000`)
   - **Apache Airflow:** `http://localhost/airflow/` (or direct port at `http://localhost:8080`)
   - **MinIO Console:** `http://localhost:9001`
   - **JupyterLab:** `http://localhost/jupyter/` (or direct port at `http://localhost:8888`)
   - **Apache Superset:** `http://localhost/superset/` (or direct port at `http://localhost:8088`)

### Install Development & Linting Tools

```bash
pip install ruff bandit[toml] pre-commit
pre-commit install
```

`pre-commit install` sets up git hooks to ensure all files are linted and formatted before commits are recorded.

---

## Code Quality & Standards

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and code formatting, configured in `pyproject.toml`.

```bash
# Check linting
ruff check .

# Auto-fix safe lint errors
ruff check --fix .

# Check formatting
ruff format --check .

# Auto-format
ruff format .
```

All CI checks must pass before a Pull Request is approved and merged. Validate them locally before pushing:

| Check | Scope | Command |
|-------|-------|---------|
| **Lint** | Python codebase | `ruff check .` |
| **Format** | Code style | `ruff format --check .` |
| **Security Scan** | Pipeline & UI | `bandit -r pipeline/ ui/ -ll -c pyproject.toml` |
| **UI Container** | UI Docker build | `docker build ui/` |
| **Helm Chart** | Kubernetes chart | `helm lint deploy/helm/lake-of-tears` |
| **Terraform** | IaC validation | `terraform -chdir=deploy/terraform/foundation validate` |

---

## Adding a New Data Source Connector

To contribute a new data source connector:

1. **Ingestion Script:** Create `pipeline/ingest/my_source.py` to fetch raw data, normalize into a pandas DataFrame, and write partitioned Parquet files via `StorageWriter`.
2. **Airflow DAG:** Create `pipeline/dags/ingest_my_source_dag.py` with an appropriate schedule and error handling.
3. **Embeddings:** Add the source identifier to `SOURCES` in `pipeline/embed/embed_sources.py`.
4. **Text Representation:** Implement `_text_my_source(row)` in `embed_sources.py` to format records for Gemini vector embeddings.
5. **Configuration:** Add required environment variables to `.env.example` and document them in `README.md`.

---

## Submitting a Pull Request

> [!IMPORTANT]
> **Mandatory Ticket Tracking Rule:** Every commit, branch, and Pull Request must be linked to an open GitHub issue/ticket. Do not start work or submit PRs without an existing ticket number to ensure complete history tracking and auditability.

For our complete branching model, release lifecycle, and commit guidelines, please review our [Branching Strategy & Release Workflow](docs/branching-strategy.md).

1. **Open or locate an issue:** Verify an open issue exists for your task, or open a new one first.
2. **Fork** the repository to your own GitHub account: [github.com/Lake-of-Tears/lake-of-tears](https://github.com/Lake-of-Tears/lake-of-tears).
3. **Branch** off from `main`:
   ```bash
   git checkout main && git pull origin main
   git checkout -b feat/123-my-new-connector
   ```
3. **Follow branch naming conventions** (include the GitHub issue number):
   - `feat/<issue#>-<description>` — new features or connectors
   - `fix/<issue#>-<description>` — bug fixes
   - `sec/<issue#>-<description>` — security fixes & CVE updates
   - `infra/<issue#>-<description>` — Docker, Helm, CI/CD, or Terraform changes
   - `docs/<description>` — documentation improvements
   - `refactor/<description>` — code cleanups and refactoring
4. **Follow Conventional Commits:** Use standard commit prefixes (`feat:`, `fix:`, `sec:`, `docs:`, `infra:`, `refactor:`).
5. **Keep PRs focused:** Submit atomic pull requests addressing one feature, bug fix, or task.
6. **Link related issues:** Include `Closes #<issue>` or `Fixes #<issue>` in your PR description.
7. **Test thoroughly:** Ensure all local checks and CI steps pass.
8. **Fill out the Pull Request template:** Describe what changes were made, why, and test verification details.

---

## Code of Conduct

We are committed to providing a welcoming, diverse, and inclusive environment. Please treat all contributors and maintainers with respect. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for full details.

---

## Reporting Security Issues

Please **do not** open public GitHub issues for security vulnerabilities.

If you discover a vulnerability or security flaw, please report it privately to:
- **Email:** [support@thelakeoftears.com](mailto:support@thelakeoftears.com)
- **Subject:** `[Security] Lake of Tears Vulnerability Report`

Include details on reproducing the issue and the potential impact. We will respond promptly and coordinate a fix and responsible disclosure. See [SECURITY.md](SECURITY.md) for more details.

