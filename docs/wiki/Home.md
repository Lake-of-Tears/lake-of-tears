# Lake of Tears Docs

**Lake of Tears** is an open-source, self-hosted datalakehouse: one URL, one sidebar, and all your data tools in one place. It combines S3-compatible object storage, a DuckDB SQL engine, AI-powered natural-language search, automated pipelines and BI dashboards, without an enterprise price tag. Think of it as an open, self-hosted take on the Databricks workflow deployed natively on Kubernetes with Helm.

## What it gives you

- **One entry point.** A Kubernetes Ingress on port 80 fronts the Lake UI, the API, Apache Superset and Apache Airflow. Notebooks (JupyterLab) are reached through the Lake UI.
- **SQL over Parquet.** DuckDB queries Hive-partitioned Parquet files straight from MinIO, with no separate query server.
- **Catalogs and workspaces.** Teams get isolated workspaces that own catalogs of schemas and tables, which can be shared read-only between workspaces.
- **AI-assisted search.** Embeddings plus DuckDB's vector search power natural-language questions over your data.
- **Scheduled pipelines.** Airflow DAGs ingest data, create embeddings, detect anomalies and keep catalogs tidy.
- **Sign-in options.** Email and password, plus SSO through Google, GitHub, Microsoft or any OIDC provider.

## Guides

| Guide | What you will find |
|---|---|
| [Architecture Overview](Architecture-Overview.md) | How the pieces fit together: the DuckDB-first model, the storage layout, request routing, the component map and Kubernetes deployment |
| [Deployment and Operations](Deployment-and-Operations.md) | Unified TOON platform configuration, local and production deployment with Helm, zero-disk CI/CD streaming, health checks, backups, storage maintenance, and secret rotation |
| [Data Ingestion and Pipelines](Data-Ingestion-and-Pipelines.md) | How data flows from a source into MinIO as Parquet, a tested step-by-step tutorial for writing a new ingest script and Airflow DAG, and best practices for idempotency, backfills, schema changes and error handling |
| [Airflow Orchestration](Airflow-Orchestration.md) | How Airflow is set up in Helm, the DAGs that ship, the DAG standard with a copy-and-paste template, and operations: triggering, testing, backfilling, logs, credentials and a troubleshooting checklist |
| [Chunking and Vector Embeddings](Chunking-and-Vector-Embeddings.md) | How text becomes vectors stored in Parquet, how to register a source for embedding, how long text is split into chunks (`recursive`, `document`, `agentic`), and query examples |
| [DuckDB Querying and Analytics](DuckDB-Querying-and-Analytics.md) | Where you can run SQL, connecting DuckDB to MinIO, partition-aware query patterns, vector search syntax, and using the lake from notebooks and Superset |
| [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md) | Workspace isolation, the workspace cookie, deletion and purge lifecycles, catalog sharing and administration |

More guides will appear in this list as they are published.

## Quick start

```bash
git clone https://github.com/Lake-of-Tears/lake-of-tears.git
cd lake-of-tears

# Option A: Unified TOON workflow (Recommended)
cp lake-of-tears.example.toon lake-of-tears.local.toon
helm upgrade --install lake-of-tears ./deploy/helm/lake-of-tears \
  --namespace lake-of-tears --create-namespace \
  -f <(python scripts/toon2helm.py -i lake-of-tears.local.toon)

# Option B: Standard Helm values override
# cp deploy/helm/values-local.example.yaml values-local.yaml
# helm upgrade --install lake-of-tears ./deploy/helm/lake-of-tears \
#   --namespace lake-of-tears --create-namespace \
#   -f values-local.yaml
```

Open **http://localhost** and register the first account, which becomes the administrator. Access the deployment via the Ingress entrypoint on port 80.

For Kubernetes, see the [Helm instructions in the README](https://github.com/Lake-of-Tears/lake-of-tears#deploying-with-helm-kubernetes).

## Where to go next

- New to the project? Start with the [Architecture Overview](Architecture-Overview.md).
- Running a multi-team instance? Read [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md).
- Want to contribute? See [CONTRIBUTING.md](https://github.com/Lake-of-Tears/lake-of-tears/blob/main/CONTRIBUTING.md) and the [branching strategy](https://github.com/Lake-of-Tears/lake-of-tears/blob/main/docs/branching-strategy.md).
- Found a problem? [Open an issue](https://github.com/Lake-of-Tears/lake-of-tears/issues).
