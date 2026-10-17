# Change Tracker

Every change made to this repo gets one row, newest first.

**Action types**

| Action | Use when |
|---|---|
| 20261016 | IMPROVEMENT | #161 | `CHANGES.md` | Added the missing tracker rows for #73 and #154 |
| ADD | New file, feature, doc or capability |
| REMOVE | Deleted file, feature or dead code |
| BUG FIX | Corrects incorrect behavior |
| IMPROVEMENT | Refactor, performance, security hardening, clarity; behavior otherwise unchanged |

| Date | Action | Ticket | Files | Summary |
|---|---|---|---|---|
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
