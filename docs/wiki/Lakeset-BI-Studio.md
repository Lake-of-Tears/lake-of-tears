# Lakeset BI Studio (`/lakeset`)

**Lakeset BI Studio** natively integrates Apache Superset into Lake of Tears under `/lakeset` with **Zero-Login SSO**, automated **DuckDB + MinIO S3 Lakehouse** database provisioning, **semantic catalog dataset synchronization**, and **workspace RBAC / Row-Level Security (RLS)**.

---

## 1. Architecture Pillars

### Zero-Login SSO (`LakesetSecurityManager`)
Users never encounter a separate Superset login screen:
- LakeUI verifies the user's `lake_token` session cookie and injects HMAC-signed proxy headers (`X-Lakeset-User-Email`, `X-Lakeset-User-Name`, `X-Lakeset-User-Role`, `X-Lakeset-Workspace`, `X-Lakeset-Proxy-Secret`, `X-Lakeset-Signature`) on `/lakeset/proxy/{path}`.
- `LakesetSecurityManager` (`deploy/helm/lake-of-tears/templates/superset-config.yaml` & `backend/lakeset_security.py`) validates the proxy headers and automatically provisions/updates the user in Superset on the fly.
- **Role Mapping:**
  - `superadmin` &rarr; Superset `Admin`
  - Workspace `owner`, `admin`, `editor`, `member`, `user` &rarr; Superset `Alpha`
  - Workspace `viewer`, `guest`, `readonly` &rarr; Superset `Gamma`

### Automated DuckDB + MinIO S3 Lakehouse Engine
- The Superset container (`deploy/helm/lake-of-tears/templates/superset.yaml`) automatically installs `duckdb` and `duckdb-engine`.
- `backend/superset_service.py` provisions the `Lake of Tears DuckDB Lakehouse` database connection (`duckdb:////app/superset_home/lakeset.duckdb`) pre-configured with `httpfs` and MinIO S3 credentials (`s3_endpoint`, `s3_access_key_id`, `s3_secret_access_key`, `s3_url_style='path'`).
- Workspace catalog tables are materialized as DuckDB views (`<schema_slug>.<table_slug>`) reading directly from MinIO Parquet (`read_parquet('s3://...')`).

### Catalog-to-Lakeset Semantic Sync (`POST /api/v1/lakeset/sync`)
- Synchronizes governed `CatalogTable` records in the caller's workspace (and approved `CatalogAccess` shares) as Superset Datasets.
- Pushes `column_defs` types, AI-generated column descriptions, and governed metrics (`count`, `sum__<col>`, `avg__<col>`) directly into the semantic layer.
- Automatically triggered during `angler bundle deploy` (`BundleDeployer.deploy`).

### Per-User RBAC & Guest Tokens (`POST /api/v1/lakeset/guest-token`)
- Mints short-lived `HS256` Superset Guest Tokens scoped strictly to the caller's authorized workspace datasets and Row-Level Security (RLS) rules.
- Requests for unauthorized datasets across workspace boundaries are rejected with `403 Forbidden`.

---

## 2. REST API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/v1/lakeset/status` | `GET` | Returns Lakeset Studio health, DuckDB engine status, and synced dataset counts. |
| `/api/v1/lakeset/datasets` | `GET` | Lists governed catalog datasets authorized for the caller in the active workspace. |
| `/api/v1/lakeset/sync` | `POST` | Registers the DuckDB Lakehouse database and syncs workspace tables + AI column descriptions. |
| `/api/v1/lakeset/guest-token` | `POST` | Mints a short-lived RBAC-scoped Superset Guest Token. |
| `/api/v1/lakeset/explore-data` | `POST` | Executes analytical series aggregation and synthesizes DuckDB SQL for the Chart Builder. |

---

## 3. Deep-Linking & Studio Workflows

- **Catalog Explorer:** Open any table's details drawer and click **Open in Lakeset** to launch `/lakeset?mode=explore&dataset=<catalog>.<schema>.<table>`.
- **SQL Editor:** Click **Open in Lakeset** in the SQL Editor toolbar to launch `/lakeset?mode=explore&sql=<encoded_sql>`.
- **Genie AI Analyst:** Click **Open in Lakeset &rarr;** on any Genie SQL response to explore and customize the chart in Lakeset BI Studio.
