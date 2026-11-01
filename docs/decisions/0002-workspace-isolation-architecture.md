# 0002: Workspace isolation architecture and system isolation specifications

- **Status:** Accepted and implemented across tickets #248–#259, #286, #295, #298, #304, #305, #307.
- **Tickets:** #260 (ADR and system isolation specs), #248 (data isolation), #249 (workspace boundaries), #250 (S3 prefix isolation), #252 (SQL guardrails), #253 (DuckDB view encapsulation), #254 (user workspace sharing), #255 (shared assets UI), #256 (pipeline scoping), #258 (workspace-scoped ingest), #259 (AI and vector isolation).
- **Date:** 2026-11-01

## Context

Lake of Tears originally utilized a shared object storage layout where ingestion pipelines and catalogs wrote directly to global top-level S3 prefixes (`s3://datalake/raw/<source>/` and `s3://datalake/<catalog>/<schema>/<table>/`). In this model:
1. Users across different environments (such as `Dev`, `Staging`, `Prod`) or functional departments (such as `Engineering`, `Finance`, `SecOps`) shared a single query namespace.
2. An embedded DuckDB engine with an open `httpfs` extension could query any Parquet path anywhere in the `datalake` bucket (`SELECT * FROM read_parquet('s3://datalake/**')`), creating cross-tenant data exfiltration risks.
3. Airflow DAGs, pipeline schedules, and execution runs were visible globally without organizational segmentation.
4. AI vector indexes, semantic RAG searches, and NL-to-SQL (Genie) schema prompts could inadvertently leak schemas or embeddings between projects.

To establish enterprise multi-tenancy and clean segregation between environments and departments without the operational overhead of spinning up separate physical clusters or separate object storage buckets per team, Lake of Tears required a formal **Workspace Isolation Architecture**.

Five core architectural decisions were made and implemented. They are recorded below along with the alternatives that were not chosen.

---

## Decision 1: Top-level workspace boundaries and multi-tenancy model

**Chosen: Logical workspace tenant jailing within a unified Lakehouse cluster.**
The **Workspace** is established as the primary tenant boundary. Workspaces segregate environments (such as `dev` vs. `prod`) or organizational departments (such as `analytics` vs. `marketing`). Each workspace owns its catalogs, ingestion configurations, pipelines, and dashboards. Users belong to one or more workspaces.

| | Chosen: Logical workspace jailing in unified lakehouse | Alternative: Physical cluster-per-tenant (separate instances) |
|---|---|---|
| Idea | Single control plane, backend, and object bucket; all resources segregated logically by workspace ID/slug | Deploying isolated Kubernetes namespaces, Helm releases, and MinIO instances for each tenant |
| For | Zero infrastructure duplication; minimal memory/CPU footprint; instant workspace provisioning; cross-workspace sharing without data replication | Complete OS and process isolation |
| Against | Requires strict software guardrails at storage, API, SQL, and UI layers to prevent cross-tenant leaks | Extreme resource overhead; complex maintenance; expensive idle compute; sharing data between teams requires bucket-to-bucket sync |
| To revert | Deploy multiple Helm chart instances pointing to distinct MinIO buckets per tenant, deprecating the workspace context middleware. | n/a |

---

## Decision 2: Storage prefix partitioning and S3 jailing

**Chosen: Strict prefix partitioning under `s3://datalake/workspaces/<workspace_slug>/`.**
All Parquet files, raw ingest records, vector embeddings, and catalog table data are strictly jailed inside the workspace's designated key prefix:
- Raw Ingestion: `s3://datalake/workspaces/<workspace_slug>/raw/<source>/year=YYYY/month=MM/day=DD/data.parquet`
- Vector Embeddings: `s3://datalake/workspaces/<workspace_slug>/embeddings/<table_slug>/data.parquet`
- Managed Catalog Tables: `s3://datalake/workspaces/<workspace_slug>/<catalog>/<schema>/<table>/data.parquet`

The system retains legacy global prefixes (`raw/`, `embeddings/`) exclusively as read-only system fallbacks for backwards compatibility.

| | Chosen: S3 prefix partitioning (`workspaces/<slug>/`) | Alternative: Separate S3 bucket per workspace |
|---|---|---|
| Idea | Single bucket (`datalake`) with top-level `workspaces/<workspace_slug>/` prefix enforcement | Dynamically creating a MinIO bucket `lake-<workspace_slug>` for every workspace |
| For | Works with standard S3/MinIO quotas; simplified IAM credentials; no bucket creation permissions needed at runtime; instant provisioning | Cloud-provider IAM policies can natively bind to buckets |
| Against | Requires application-level path validation before executing file reads | MinIO and S3 bucket limits (often 100 buckets per account); elevated IAM permissions required; complex dynamic bucket lifecycle management |
| To revert | Update `StorageWriter` and `sql_guardrails.py` to route to dynamic bucket names rather than key prefixes. | n/a |

---

## Decision 3: DuckDB SQL guardrails and view encapsulation

**Chosen: AST-level SQL validation with `sqlglot` fail-closed enforcement.**
Because DuckDB executes in-process and has full access to `httpfs` credentials to read from MinIO, raw user SQL queries cannot be executed directly against storage without verification.
1. **SQLGuardrails Engine (`backend/sql_guardrails.py` & `ui/sql_guardrails.py`):**
   - Parses incoming SQL statements into an Abstract Syntax Tree (AST) using `sqlglot` (configured with DuckDB dialect).
   - Walks table expressions, identifiers, string literals, and table-reading functions (`read_parquet`, `parquet_scan`, `scan_parquet`, `read_csv`, `read_json`, `delta_scan`, `iceberg_scan`, `glob`).
   - Validates that every extracted S3 URI resides strictly inside `s3://datalake/workspaces/<active_workspace_slug>/` or matches an authorized catalog `s3_path_pattern` / `s3_vector_path`.
   - Rejects directory traversal attempts (`..`) via POSIX path normalization.
   - **Fail-Closed Policy:** Queries that fail AST parsing but contain S3 references immediately raise `SQLGuardrailViolation` (HTTP 403 Forbidden).
2. **View Encapsulation:**
   - Catalog tables are registered into the DuckDB session as logical views pointing to their authorized Parquet locations.
   - Users query catalog views by name (`SELECT * FROM catalog.schema.table`) without directly manipulating underlying storage paths.

| | Chosen: AST SQL guardrails + catalog view encapsulation | Alternative: Separate DuckDB server instances per user/workspace |
|---|---|---|
| Idea | Single pooled DuckDB connection engine per UI replica with AST-level path inspection and access validation | Launching an isolated container or sidecar per workspace running DuckDB as a service |
| For | Preserves the embedded, ultra-fast zero-network DuckDB execution model; low memory overhead; shared memory cache | Removes need for AST inspection; physical process boundary |
| Against | Requires maintaining SQL AST validation rules and parser updates | Destroys the in-process speed of DuckDB; high memory footprint per idle workspace; high orchestration complexity |
| To revert | Remove `validate_sql_s3_paths` checks and run independent DuckDB daemon processes per workspace. | n/a |

---

## Decision 4: Simplified Role-Based Access Control (RBAC)

**Chosen: Two-tier RBAC (Global Platform Roles + Scoped Workspace Roles).**
1. **Global Roles (`User.role`):**
   - `superadmin`: Full platform control. Can create/delete/purge workspaces, manage system settings, inspect all platform metadata, and configure global AI providers and integrations.
   - `user`: Standard platform user with access only to workspaces where they hold membership or approved shares.
2. **Workspace Roles (`WorkspaceMember.role`):**
   - `admin`: Can invite/remove workspace members, configure workspace settings, create/delete owned catalogs, approve/reject inbound catalog share requests, and manage dashboards.
   - `write`: Can create/edit catalog schemas and tables, trigger ingestion and pipeline runs, run notebook executions, and detect anomalies.
   - `read`: Can browse catalogs, execute queries within SQL guardrails, view dashboards, and inspect pipeline logs in read-only mode.

| | Chosen: Two-tier platform + workspace RBAC | Alternative: Fine-grained ACLs / permission matrices |
|---|---|---|
| Idea | Fixed global roles (`superadmin`, `user`) and fixed workspace roles (`admin`, `write`, `read`) | Complex object-level ACLs (e.g. `catalog:table:read`, `pipeline:dag:execute`) stored per user |
| For | Clear, predictable permissions; easy to audit and reason about; zero performance penalty in auth middleware | Extreme granularity down to individual column or DAG levels |
| Against | Cannot grant write access to one catalog while keeping another catalog read-only in the same workspace | Overwhelming administrative complexity; high database join overhead on every API and UI route |
| To revert | Migrate `workspace_members.role` to an ACL permission table with relation grants. | n/a |

---

## Decision 5: Workspace and asset sharing lifecycle

**Chosen: User-level workspace shares and workspace-to-workspace catalog federation.**
Rather than copying Parquet data when collaboration is required, Lake of Tears implements non-duplicative, read-only federated sharing:
1. **Catalog Access Grants (`CatalogAccess`):**
   - Workspace admins request read access to a catalog owned by another workspace.
   - Owning workspace admins review and approve/reject the request.
   - Approved catalogs appear in the consumer workspace as `<owning_workspace>.<catalog_name>`. The SQL Guardrail engine automatically permits the underlying S3 paths and vector index paths for the consumer workspace.
2. **User Workspace Sharing (`WorkspaceShare`):**
   - Users can be granted shared guest access to workspaces with status (`pending`, `approved`, `rejected`).
   - The UI distinguishes native workspace objects from shared assets using visual badges (`🔗 Shared · <workspace_name>`).
   - A global **Hide Shared Assets** toggle allows users to declutter views and focus solely on native assets.
   - **Disconnect Share Flow:** Users or admins can disconnect shares at any time, immediately revoking access grants without touching source data.

| | Chosen: Federated read-only grants with guardrail expansion | Alternative: Cross-workspace data replication (ETL copy) |
|---|---|---|
| Idea | Reference-based sharing: grant read access to existing S3 paths and register views in consumer workspace | Airflow DAG copies Parquet files into consumer workspace prefix `workspaces/<consumer>/` |
| For | Instant access; zero storage cost; zero synchronization lag; single source of truth | Physical data isolation; consumer can modify data without affecting owner |
| Against | Owner must maintain data availability for consumers; requires guardrails to allow authorized external paths | 2x storage cost; data drift and stale caches; high pipeline maintenance |
| To revert | Remove `allowed_patterns` in SQL guardrails and replace catalog sharing with automated S3 file synchronization jobs. | n/a |

---

## System Isolation Implementation Summary

| Subsystem | Isolation Mechanism | Implementation Location |
|---|---|---|
| **Ingestion Storage** | Jailed prefix: `s3://datalake/workspaces/<slug>/raw/<source>/` | `pipeline/shared/storage_writer.py`, `pipeline/ingest/` |
| **Vector Embeddings** | Jailed prefix: `s3://datalake/workspaces/<slug>/embeddings/<table_slug>/` | `backend/vector_index_service.py` |
| **SQL Engine** | AST traversal & path validation via `sqlglot` | `backend/sql_guardrails.py`, `ui/sql_guardrails.py` |
| **Pipelines & DAGs** | Prefix scoping (`ws_{slug}_`) and metadata tagging (`workspace:{slug}`) | `backend/pipeline_service.py`, `pipeline/dags/` |
| **AI & Genie SQL** | Dynamic schema context strictly bounded to workspace + approved catalogs | `backend/ai_service.py`, `ui/duckdb_ai.py` |
| **UI Presentation** | Badging (`🔗 Shared`), segregated sections, and global visibility toggle | `ui/templates/`, `ui/static/css/`, `ui/static/js/` |
| **Lifecycle & Purging** | Grace periods and automated Airflow purge DAGs (`purge_inactive_workspaces`) | `pipeline/dags/purge_inactive_workspaces_dag.py` |

---

## References

- [Architecture Overview](../wiki/Architecture-Overview.md)
- [Workspaces and Catalog Management Guide](../wiki/Workspaces-and-Catalog-Management.md)
- [Airflow Orchestration Guide](../wiki/Airflow-Orchestration.md)
- [AI Providers ADR 0001](0001-ai-provider-architecture.md)
