# Lake of Tears Docs

**Lake of Tears** is an open-source, self-hosted datalakehouse: one URL, one sidebar, and all your data tools in one place. It combines S3-compatible object storage, a DuckDB SQL engine, AI-powered natural-language search, automated pipelines and BI dashboards, without an enterprise price tag. Think of it as an open, self-hosted take on the Databricks workflow that runs on a single machine with Docker Compose or on Kubernetes with Helm.

## What it gives you

- **One entry point.** An nginx reverse proxy on port 80 fronts the Lake UI, the API, Apache Superset and Apache Airflow. Notebooks (JupyterLab) are reached through the Lake UI.
- **SQL over Parquet.** DuckDB queries Hive-partitioned Parquet files straight from MinIO, with no separate query server.
- **Catalogs and workspaces.** Teams get isolated workspaces that own catalogs of schemas and tables, which can be shared read-only between workspaces.
- **AI-assisted search.** Embeddings plus DuckDB's vector search power natural-language questions over your data.
- **Scheduled pipelines.** Airflow DAGs ingest data, create embeddings, detect anomalies and keep catalogs tidy.
- **Sign-in options.** Email and password, plus SSO through Google, GitHub, Microsoft or any OIDC provider.

## Guides

| Guide | What you will find |
|---|---|
| [Architecture Overview](Architecture-Overview.md) | How the pieces fit together: the DuckDB-first model, the storage layout, request routing, the component map and the two deployment models |
| [Deployment and Operations](Deployment-and-Operations.md) | Local setup with Docker Compose, production deployment with Helm, health checks, backups and restores, storage maintenance, secret rotation and a troubleshooting table |
| [Data Ingestion and Pipelines](Data-Ingestion-and-Pipelines.md) | How data flows from a source into MinIO as Parquet, a tested step-by-step tutorial for writing a new ingest script and Airflow DAG, and best practices for idempotency, backfills, schema changes and error handling |
| [Airflow Orchestration](Airflow-Orchestration.md) | How Airflow is set up in Compose and Helm, the DAGs that ship, the DAG standard with a copy-and-paste template, and operations: triggering, testing, backfilling, logs, credentials and a troubleshooting checklist |
| [Chunking and Vector Embeddings](Chunking-and-Vector-Embeddings.md) | How text becomes vectors stored in Parquet, how to register a source for embedding, how chunking works today and what is planned, and query examples |
| [DuckDB Querying and Analytics](DuckDB-Querying-and-Analytics.md) | Where you can run SQL, connecting DuckDB to MinIO, partition-aware query patterns, vector search syntax, and using the lake from notebooks and Superset |
| [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md) | Workspace isolation, the workspace cookie, deletion and purge lifecycles, catalog sharing and administration |

More guides will appear in this list as they are published.

## Quick start

```bash
git clone https://github.com/Lake-of-Tears/lake-of-tears.git
cd lake-of-tears
cp .env.example .env
# Edit .env: set MINIO_ROOT_PASSWORD, AUTH_SECRET_KEY and POSTGRES_AUTH_PASSWORD
docker compose up -d
```

Open **http://localhost** and register the first account, which becomes the administrator. Use port 80 rather than a service's direct port: the direct UI port (3000) does not proxy `/api/`, so signing in fails there.

For Kubernetes, see the [Helm instructions in the README](https://github.com/Lake-of-Tears/lake-of-tears#deploying-with-helm-kubernetes).

## Where to go next

- New to the project? Start with the [Architecture Overview](Architecture-Overview.md).
- Running a multi-team instance? Read [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md).
- Want to contribute? See [CONTRIBUTING.md](https://github.com/Lake-of-Tears/lake-of-tears/blob/main/CONTRIBUTING.md) and the [branching strategy](https://github.com/Lake-of-Tears/lake-of-tears/blob/main/docs/branching-strategy.md).
- Found a problem? [Open an issue](https://github.com/Lake-of-Tears/lake-of-tears/issues).
