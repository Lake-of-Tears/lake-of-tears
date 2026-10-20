# Change Tracker

Every change made to this repo gets one row, newest first.

**Action types**

| Action | Use when |
|---|---|
| ADD | New file, feature, doc or capability |
| REMOVE | Deleted file, feature or dead code |
| BUG FIX | Corrects incorrect behavior |
| IMPROVEMENT | Refactor, performance, security hardening, clarity; behavior otherwise unchanged |

| Date | Action | Ticket | Files | Summary |
|---|---|---|---|---|
| 20261020 | ADD | #250 | `backend/storage_service.py`, `backend/storage_router.py`, `backend/catalog_storage.py`, `ui/main.py`, `ui/templates/storage.html`, `tests/` | Workspace isolation for S3/MinIO storage prefix jailing (`workspaces/<slug>/`): dual-mode path resolution, strict traversal/cross-workspace 403 rejection, prefix-scoped metrics, direct & presigned file uploads, and UI upload modal |
| 20261019 | IMPROVEMENT | #211 | `backend/alembic/env.py`, `backend/database.py`, `backend/requirements.txt`, `tests/test_database_url.py` | Migrated to SQLAlchemy 2.1.5 and psycopg v3 (`psycopg[binary] 3.3.6`); added `normalize_database_url()` to keep plain `postgresql://` URLs in Compose, Helm and Airflow (PR #224). Note: local dev environments need `pip install -r backend/requirements.txt` |
| 20261017 | BUG FIX | #189 | `ui/duckdb_ai.py`, `ui/main.py`, `ui/templates/sql.html`, `tests/test_ui_sql_table_names.py` | SQL Editor sidebar, Query button and AI schema prompt now use the DuckDB view name (`media.tmdb_trending`) instead of the hyphenated catalog slug, which was invalid SQL (PR #190) |
| 20261017 | ADD | #191 | `docs/wiki/Airflow-Orchestration.md` | Documented which workspace sees which pipeline (prefix, tag, `run_notebook`) and that superadmins see all by design (PR #192) |
| 20261017 | ADD | #71 | `docs/wiki/AI-Providers.md`, `docs/wiki/pages.json`, `docs/wiki/Architecture-Overview.md`, `docs/wiki/Chunking-and-Vector-Embeddings.md`, `docs/wiki/Deployment-and-Operations.md`, `docs/wiki/DuckDB-Querying-and-Analytics.md` | Added the AI Providers guide and pointers from the other guides (PR #186) |
| 20261017 | ADD | #71 | `backend/ai_providers/bedrock.py`, `backend/ai_providers/registry.py`, `README.md`, `tests/test_ai_bedrock.py` | Amazon Bedrock provider: Converse chat, Titan and Cohere embeddings, IAM-role or static-key auth (PR #185) |
| 20261017 | ADD | #70 | `backend/ai_providers/http_providers.py`, `backend/ai_providers/registry.py`, `.env.example`, `README.md`, `tests/test_ai_openai_azure.py` | OpenAI and Azure AI Foundry providers with Admin Panel forms and environment fallbacks (PR #184) |
| 20261017 | ADD | #69 | `backend/ai_providers/http_providers.py`, `docker-compose.ollama.yml`, `README.md`, `tests/test_ai_http_providers.py` | Ollama and OpenAI-compatible providers (dimension auto-detected, optional query/document prefixes) and an optional Ollama Compose file (PR #182) |
| 20261017 | ADD | #71 | `backend/ai_admin_router.py`, `backend/ai_providers/registry.py`, `ui/main.py`, `ui/templates/settings/admin.html`, `tests/test_ai_admin.py` | Admin Panel AI Provider selector with schema-driven fields, encrypted secrets and Test Connection (PR #181) |
| 20261017 | IMPROVEMENT | #68 | `backend/ai_providers/compat.py`, `backend/ai_service.py`, `pipeline/shared/ai_gateway.py`, `pipeline/embed/*.py`, `pipeline/query/rag_query.py`, `ui/ai_gateway_client.py` | Backend, UI and pipelines call the provider gateway instead of the Gemini SDK; embeddings record provider, model and dimension and search filters by them (PR #180) |
| 20261017 | ADD | #68 | `backend/ai_providers/`, `backend/ai_gateway_router.py`, `docs/decisions/0001-ai-provider-architecture.md`, `tests/test_ai_providers.py` | Provider interfaces, Gemini provider, registry, encrypted settings and internal AI gateway; decision record with the alternatives not chosen (PR #179) |
| 20261017 | ADD | #75, #76, #77, #78 | `docs/wiki/Airflow-Orchestration.md`, `docs/wiki/Chunking-and-Vector-Embeddings.md`, `docs/wiki/DuckDB-Querying-and-Analytics.md`, `docs/wiki/Deployment-and-Operations.md`, `docs/wiki/pages.json` | Added the Airflow, Chunking and Embeddings, DuckDB and Deployment and Operations guides (PR #178) |
| 20261017 | BUG FIX | #171 | `backend/ai_service.py`, `ui/duckdb_ai.py`, `ui/main.py`, `tests/test_ai_vector_search_schema.py` | AI Query now reads the embeddings table's real columns and embeds questions at 768 dimensions (PR #173) |
| 20261017 | BUG FIX | #165 | `docker-compose.yml`, `deploy/helm/lake-of-tears/templates/airflow.yaml`, `.env.example`, `README.md`, `tests/test_airflow_admin_password_config.py` | Airflow admin password from `AIRFLOW_PASSWORD` is applied on every start (reset-password, stale pid file removed) (PR #167) |
| 20261017 | BUG FIX | #163 | `docker-compose.yml`, `deploy/nginx/lake.conf`, `deploy/helm/lake-of-tears/templates/airflow.yaml`, `tests/test_airflow_proxy_config.py` | Airflow web UI now starts and keeps its `/airflow` prefix through nginx, fixing the 502 (PR #164) |
| 20261017 | IMPROVEMENT | #193 | `CHANGES.md` | Added the tracker rows for the open fixes, wiki guides and AI provider PRs |
| 20261016 | BUG FIX | #169 | `deploy/helm/lake-of-tears/templates/airflow.yaml`, `deploy/helm/values-local.yaml`, `ui/templates/pipelines.html` | Fixed pipeline drawer double scrollbar; tuned Airflow webserver workers and increased local memory limit to 4Gi |
| 20261016 | IMPROVEMENT | #161 | `CHANGES.md` | Added the missing tracker rows for #73 and #154 |
| 20261016 | ADD | #74 | `docs/wiki/Data-Ingestion-and-Pipelines.md`, `docs/wiki/Home.md`, `docs/wiki/pages.json` | Added Data Ingestion and Pipeline Authoring guide covering architecture, storage layout, tutorial and best practices (PR #160) |
| 20261016 | IMPROVEMENT | #157 | `README.md`, `deploy/helm/lake-of-tears/templates/NOTES.txt`, `deploy/nginx/lake.conf`, `docker-compose.yml`, `CONTRIBUTING.md` | Removed stale references to public /jupyter/ route and port 8888; gated NOTES.txt credentials on jupyter.ingress.enabled (PR #159) |
| 20261016 | IMPROVEMENT | #79 | `docs/wiki/images/admin-workspaces.jpg`, `docs/wiki/Workspaces-and-Catalog-Management.md` | Retook the Admin workspaces screenshot after #129 (Default shows System badge); replaced the known-issue note with the fixed behavior |
| 20261016 | BUG FIX | #151 | `pipeline/dags/purge_deleted_catalogs_dag.py`, `tests/test_purge_deleted_catalogs_dag.py` | Scheduled catalog purge DAG now skips S3 prefixes shared with surviving catalogs, preventing silent data deletion; added DAG unit tests (PR #152) |
| 20261016 | BUG FIX | #132 | `backend/main.py` | Admin hard delete (`DELETE /api/admin/workspaces/{id}`, `mode=hard`) now deletes MinIO objects for every owned catalog before the DB rows (502 on storage failure); skips prefixes shared with other workspaces |
| 20261016 | BUG FIX | #132 | `pipeline/dags/purge_inactive_workspaces_dag.py` | Prefix derivation never matched real `s3://bucket/.../*.parquet` patterns, so the DAG deleted nothing; now correct, with shared-prefix protection |
| 20261016 | ADD | #132 | `tests/test_workspace_purge_objects.py` | Tests for workspace object purge, shared-prefix sparing, 502 path and ordering |
| 20261016 | BUG FIX | #129 | `backend/main.py`, `backend/alembic/versions/0007_backfill_default_workspace_is_system.py`, `tests/test_default_workspace_system.py` | Default workspace now created with `is_system=True`; migration 0007 backfills existing installs (PR #130) |
| 20261016 | REMOVE | #126 | `backend/main.py`, `tests/test_workspace_delete_routes.py` | Removed legacy `DELETE /api/workspaces/{id}` route that bypassed the admin delete safeguards (PR #131) |
| 20261016 | IMPROVEMENT | #79 | `docs/wiki/Workspaces-and-Catalog-Management.md` | Added note: log in via http://localhost (port 80), not :3000, which does not proxy /api/ |
| 20261016 | ADD | #73 | `docs/wiki/Home.md`, `docs/wiki/Architecture-Overview.md`, `docs/wiki/pages.json` | Added the wiki Home and Architecture Overview pages (DuckDB-first model, storage layout, nginx routing, component map, Compose vs Helm), published on the website (PR #156) |
| 20261016 | ADD | #154 | `docs/wiki/index.html`, `docs/wiki/pages.json`, `docs/wiki/README.md`, `docs/index.html` | Added a wiki section to thelakeoftears.com: a static viewer that renders markdown from `docs/wiki/` in the browser (marked, DOMPurify, mermaid), plus "Docs" links in the main site nav and footer (PR #155) |
| 20261015 | BUG FIX | #125 | `backend/main.py`, `backend/catalog_storage.py` | `POST /api/catalogs/{id}/purge` now deletes MinIO objects before the DB rows (502 on storage failure); skips prefixes shared with other catalogs |
| 20261015 | BUG FIX | #125 | `pipeline/dags/purge_deleted_catalogs_dag.py` | Prefix derivation never matched real `s3://bucket/.../*.parquet` patterns, so the DAG deleted nothing; now derives the key prefix correctly |
| 20261015 | ADD | #125 | `tests/test_catalog_storage.py` | Unit tests for prefix derivation, shared-prefix protection and paged deletes |
| 20261015 | ADD | #125, #126 | GitHub issues | Filed ticket #125 (purge leaves MinIO objects orphaned) and #126 (legacy workspace delete endpoint bypasses safeguards) |
| 20261014 | ADD | #79 | `docs/wiki/Workspaces-and-Catalog-Management.md` | Draft wiki guide for workspaces, catalogs, sharing, purge lifecycle; flags 3 spec/code discrepancies |
| 20261014 | ADD | - | `CHANGES.md` | Created change tracker with action classification |
