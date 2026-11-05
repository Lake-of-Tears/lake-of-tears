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

- **Issue #380 & PR #385 (Secret Management Core Architecture, Data Models & Internal Encrypted DB Provider):**
  - **Status:** **COMPLETED, DEPLOYED, MERGED & CI GREEN**
  - **Pull Request:** Merged via PR #385 into `main` (`6ec52fe`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Item IDs `PVTI_lADOFEJxWM4Bmf-gzhGI8gc` and `PVTI_lADOFEJxWM4Bmf-gzhGJGno`).
  - **Database Migration:** Alembic revision `0021_secret_scopes_keys_and_acls` applied.
  - **Test Suite:** 777 tests passing (`tests/test_secret_manager.py`).
  - **Changes:**
    - Database models `SecretScope`, `SecretKey`, and `SecretAcl` added to `backend/models.py`.
    - Alembic migration `0021_secret_scopes_keys_and_acls.py` applied, creating `secret_scopes`, `secret_keys`, and `secret_acls` with unique constraints and indexing.
    - Extensible secret provider abstraction in `backend/secrets_manager/` with `BaseSecretProvider` and `InternalSecretProvider` implementing AES-256-GCM / HMAC-SHA256 envelope encryption. Plaintext secret values are never returned in list endpoints.
    - `SecretManagerService` handling Databricks-style 3-tier ACLs (`MANAGE`, `WRITE`, `READ`) per user or workspace role, with Superadmin root bypass and creator ownership.
    - REST API router in `backend/secrets_router.py` mounted at `/api/v1/secrets` and `/api/v1/workspaces/{workspace_id}/secrets`.
  - **Local Deployment:** Rebuilt image `lake-of-tears-backend:latest`; rolled out to local k8s deployment `lake-of-tears-backend` in namespace `lake-of-tears`. Verified live health check at `http://localhost/api/health` and database migration 0021 in Postgres.

  - **Status:** **COMPLETED, DEPLOYED, MERGED & CI GREEN**
  - **Pull Request:** Merged via PR #378 into `main` (`f755392`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Item ID `PVTI_lADOFEJxWM4Bmf-gzhF2YEA`).
  - **Database Migration:** Alembic revision `0020_password_reset_tokens` applied.
  - **Test Suite:** 771 tests passing (`tests/test_password_reset.py`).
  - **Changes:**
    - Right-aligned link under password input on `/login`: "Need help signing in?".
    - Glassmorphic modal overlay collecting email address, rate-limited to 3 requests per 15 minutes per IP/email.
    - Privacy-preserving generic UI response preventing email enumeration.
    - Contextual email handling: password reset link for password accounts, SSO reminder for OAuth accounts, and registration/admin guidance for unknown accounts.
    - Dev mode fallback logging reset links to console when SMTP is unconfigured.
    - Database-backed single-use SHA-256 tokens (`password_reset_tokens` table) with 30-minute expiration.
    - Immediate token verification on page load when visiting `/login?reset_token=...`.
    - Dedicated "Set a new password" form with client and server password matching and validation.
    - Full session revocation on password reset; returns to Sign In with success banner.
  - **Local Deployment:** Rebuilt images `lake-of-tears-backend:latest` and `lake-of-tears-ui:latest`; rolled out to local k8s deployments `lake-of-tears-backend` and `lake-of-tears-lakeui`. Verified live at http://localhost.


