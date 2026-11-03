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

