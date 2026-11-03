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
  - **Status:** **COMPLETED & DEPLOYED LOCALLY**
  - **Pull Request:** Merged via PR #374 into `main` (`3fc9952`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3).
  - **Database Migration:** Alembic revision `0019_catalog_access_user_scoped` applied to local Postgres cluster.
  - **Local Cluster Status:**
    - Deployment rolled out to local k8s (`lake-of-tears-backend` and `lake-of-tears-lakeui`).
    - App running at http://localhost.
  - **Automated Tests:**
    - Integration test suite: `tests/test_catalog_access_user_scoping.py` (passes 100%).
    - Full repo test suite: 758 tests passing.
  - **Testing Plan When Resuming:**
    - Open http://localhost in browser.
    - Test user isolation between two accounts in `dev` workspace (`test@gmail.com` and `developer@thelakeoftears.com`):
      1. User A requests access to an external catalog owned by `Default`.
      2. Owner admin (`hubbertj@gmail.com`) approves User A.
      3. Verify User A sees the catalog under *Shared with me*, *Catalog*, *SQL Editor*; User B does not.
      4. User B independently requests access to the same catalog.
      5. User A self-service removes their share via *Shared with me* without affecting User B.
      6. Owner admin revokes User A / User B shares individually in *Shared with others*.

