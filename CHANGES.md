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
| 20261016 | IMPROVEMENT | #79 | `docs/wiki/Workspaces-and-Catalog-Management.md` | Added note: log in via http://localhost (port 80), not :3000, which does not proxy /api/ |
| 20261015 | BUG FIX | #125 | `backend/main.py`, `backend/catalog_storage.py` | `POST /api/catalogs/{id}/purge` now deletes MinIO objects before the DB rows (502 on storage failure); skips prefixes shared with other catalogs |
| 20261015 | BUG FIX | #125 | `pipeline/dags/purge_deleted_catalogs_dag.py` | Prefix derivation never matched real `s3://bucket/.../*.parquet` patterns, so the DAG deleted nothing; now derives the key prefix correctly |
| 20261015 | ADD | #125 | `tests/test_catalog_storage.py` | Unit tests for prefix derivation, shared-prefix protection and paged deletes |
| 20261015 | ADD | #125, #126 | GitHub issues | Filed ticket #125 (purge leaves MinIO objects orphaned) and #126 (legacy workspace delete endpoint bypasses safeguards) |
| 20261014 | ADD | #79 | `docs/wiki/Workspaces-and-Catalog-Management.md` | Draft wiki guide for workspaces, catalogs, sharing, purge lifecycle; flags 3 spec/code discrepancies |
| 20261014 | ADD | - | `CHANGES.md` | Created change tracker with action classification |
