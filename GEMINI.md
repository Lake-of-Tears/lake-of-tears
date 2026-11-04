# Lake of Tears Agent Instructions & Rules

## Project Kanban Board & Task Lifecycle
- **Project Board:** [Lake of Tears (Main)](https://github.com/orgs/Lake-of-Tears/projects/3)
  - Project Number: `3`
  - Owner: `Lake-of-Tears`
  - Project ID: `PVT_kwDOFEJxWM4Bmf-g`
  - Status Field ID: `PVTSSF_lADOFEJxWM4Bmf-gzhlIwtk`
    - `Todo`: `f75ad846`
    - `In progress`: `47fc9ee4`
    - `Done`: `98236657`

## Task Completion Rules
- Whenever an issue, PR, or task is closed/completed:
  1. Ensure the item is on the board:
     ```bash
     gh project item-add 3 --owner Lake-of-Tears --url <issue_or_pr_url>
     ```
  2. Move the item to the **Done** column:
     ```bash
     gh project item-edit --id <item_id> --field-id PVTSSF_lADOFEJxWM4Bmf-gzhlIwtk --single-select-option-id 98236657 --project-id PVT_kwDOFEJxWM4Bmf-g
     ```

## Current Project State & Next Steps (as of Nov 3, 2026)
- **Issue #294 (User-Scoped Catalog Access & Grants):**
  - **Status:** **COMPLETED, VERIFIED & MERGED**
  - **Pull Request:** Merged via PR #374 into `main` (`3fc9952`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3).
  - **Database Migration:** Alembic revision `0019_catalog_access_user_scoped` applied.
  - **Test Suite:** 758 tests passing (`tests/test_catalog_access_user_scoping.py`).

- **Issue #375 (User Menu Refinement & Light Mode Theme Fixes):**
  - **Status:** **COMPLETED, DEPLOYED & MERGED**
  - **Pull Request:** Merged via PR #376 into `main` (`62fe863`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3).
  - **Changes:**
    - Unified user menu labels (removed redundant "settings" wording: `Account`, `Workspace`, `Admin`).
    - Added user avatar initials, user email, and LoT role status pill badge with accent dot.
    - Fixed light mode styling for user menu and workspace dropdowns (now clean `#ffffff` cards).
    - Fixed theme switcher contrast (segmented pill for active mode).
    - Enhanced search input and topbar contrast in light mode.
  - **Local Deployment:** Rebuilt `lake-of-tears-ui:latest` and rolled out to local k8s deployment `lake-of-tears-lakeui`. Running at http://localhost.

- **Issue #245 (Pluggable Chunking and Lineage Columns in Table Vector Sync):**
  - **Status:** **COMPLETED, DEPLOYED, MERGED & CI GREEN**
  - **Pull Request:** Merged via PR #377 into `main` (`1dd47ed`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Item ID `PVTI_lADOFEJxWM4Bmf-gzhB5SFc`).
  - **CI Workflow Hardening:** Fixed Trivy installation in GitHub Actions (`.github/workflows/ci.yml`) by pulling directly from official GitHub releases to avoid upstream DNS outages on `get.trivy.dev`. CI on `main` is 100% green (run `41747619823`).
  - **Changes:**
    - Pluggable chunkers (`RecursiveChunker`, `DocumentChunker`, `AgenticChunker`) integrated into table vector indexing.
    - Output Parquet schema standardized: `["id", "source", "content", "embedding", "source_date", "provider", "model", "dimension", "parent_id", "chunk_index", "total_chunks", "metadata", "ingested_at"]`.
    - Lineage preservation: single chunks retain `id == parent_id`, multi-chunk rows receive `<parent_id>:<chunk_index>` IDs.
    - Metadata serialization of non-indexed columns with oversized values truncated to 200 chars.
    - Catalog drawer UI controls added for Chunking Strategy, Chunk Size, and Chunk Overlap, plus Strategy display pill in status overview.
  - **Local Deployment:** Rebuilt images `lake-of-tears-backend:latest` and `lake-of-tears-ui:latest`; rolled out to local k8s deployments `lake-of-tears-backend` and `lake-of-tears-lakeui`. Verified live table sync and multi-chunk explosion on `tmdb-trending`.


