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

## Current Project State & Next Steps (as of Nov 5, 2026)
- **Issue #296 & PR #391 (Helm Lifecycle Scripts: install, update, and destroy with TOON configuration):**
  - **Status:** **COMPLETED, MERGED & ALL 814 TESTS PASSING**
  - **Pull Request:** Merged via PR #391 into `main` (`b265ba6`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Issue item `PVTI_lADOFEJxWM4Bmf-gzhDSjFU`, PR item `PVTI_lADOFEJxWM4Bmf-gzhGVDUM`).
  - **Test Suite:** 814 tests passing (`tests/test_helm_lifecycle_scripts.py`).
  - **Changes:**
    - Standardized lifecycle operations around Lake of Tears' **TOON (Token-Oriented Object Notation)** specification (`lake-of-tears.toon`) and dynamic Helm compiler (`scripts/toon2helm.py`) with zero plaintext secret persistence to disk.
    - Unified root CLI runner [`./lot`](file:///Users/Jay/Development/lake-of-tears/lot) with `install`, `update`, `destroy`, `status`, and `config` commands.
    - Direct executable scripts in `deploy/scripts/`:
      - [`deploy/scripts/install.sh`](file:///Users/Jay/Development/lake-of-tears/deploy/scripts/install.sh): Checks prerequisites, multi-tier Python runtime resolution, auto-generates secure random credentials for `changeme-*` placeholders, manages namespace creation, compiles TOON to Helm dynamically, polls live `/api/health`, and renders a clean terminal deployment summary table with Ingress URLs and Day-0 superadmin instructions.
      - [`deploy/scripts/update.sh`](file:///Users/Jay/Development/lake-of-tears/deploy/scripts/update.sh): Implements a TrueNAS-style update architecture supporting `stable` (release tags) and `edge` (`origin/main`) trains, `--check-only`, and machine-readable `--json` output for future Lake UI Admin Panel (`/settings/admin#updates`) integration. Performs atomic rolling updates (`--atomic --timeout 10m`) with automated rollback to prior working revision on failure.
      - [`deploy/scripts/destroy.sh`](file:///Users/Jay/Development/lake-of-tears/deploy/scripts/destroy.sh): Safe-by-default uninstallation that preserves PersistentVolumeClaims (PVCs) for MinIO, Postgres, Airflow, and Jupyter notebooks. Complete data purging requires explicit selection and typing confirmation of the namespace name (`lake-of-tears`) to prevent accidental data loss (bypassed with `--force`).
      - [`deploy/scripts/status.sh`](file:///Users/Jay/Development/lake-of-tears/deploy/scripts/status.sh): Inspects active Helm release, pod workload phases, and resolved service URLs.
      - [`deploy/scripts/common.sh`](file:///Users/Jay/Development/lake-of-tears/deploy/scripts/common.sh): Shared shell library for color logging, runtime resolution, cluster validation, and secret handling.
    - Added [`pipeline/shared/lifecycle.py`](file:///Users/Jay/Development/lake-of-tears/pipeline/shared/lifecycle.py) and [`scripts/lifecycle_helper.py`](file:///Users/Jay/Development/lake-of-tears/scripts/lifecycle_helper.py) for config resolution, secret scanning/generation, and TrueNAS-style version checks.
    - Updated `README.md` and `deploy/helm/README.md` with `./lot` usage documentation.

- **Issue #269 & PR #390 (Platform Service & Process Management and Tabbed Admin Panel UI):**
  - **Status:** **COMPLETED, DEPLOYED, MERGED & ALL 806 TESTS PASSING**
  - **Pull Request:** Merged via PR #390 into `main` (`4537efa`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Issue item `PVTI_lADOFEJxWM4Bmf-gzhCTUsA`, PR item `PVTI_lADOFEJxWM4Bmf-gzhGSmKQ`).
  - **Test Suite:** 806 tests passing (`tests/test_service_management.py`).
  - **Changes:**
    - Expanded Admin Panel width from cramped `max-w-3xl` (768px) to `max-w-6xl` responsive wide layout.
    - Added horizontal tab navigation with bidirectional URL hash/query sync (`#services`, `#users`, `#workspaces`, `#vaults`, `#platform`).
    - Kubernetes in-cluster service observability and lifecycle API in `backend/services_manager/` and `backend/services_admin_router.py`:
      - `GET /api/admin/services`: Lists all platform deployments, pod phase, replicas, restart counts, and container specs.
      - `POST /api/admin/services/{service_name}/restart`: Rollout restart via deployment annotation patch.
      - `POST /api/admin/services/{service_name}/stop`: Scale deployment replicas to 0. Core services (`backend`, `lakeui`) are strictly safeguarded with `400 Bad Request`.
      - `POST /api/admin/services/{service_name}/start`: Scale deployment replicas to 1.
    - Helm RBAC template `deploy/helm/lake-of-tears/templates/rbac.yaml` creating Role and RoleBinding for backend ServiceAccount in `lake-of-tears` namespace.
    - Interactive Services UI with status badges, replica counters, uptime, limits, action buttons with loading spinners, confirmation modal for disruptive actions, and 10s auto-polling toggle.
    - **Minimalist List View & Slide-Over Drawer**: Upgraded `#services` tab from a bulky 3-column card grid to a streamlined, responsive table list view. Clicking any service row opens a slide-over details drawer with full runtime pod specs (namespace, node, pod name, container image, port, resource limits, uptime, last restart) and lifecycle action buttons (Restart, Stop with core safeguards, Start). Drawer live-syncs with the 10s auto-poll loop and closes with `Escape` or backdrop click.
  - **Local Deployment:** Rebuilt images `lake-of-tears-backend:latest` and `lake-of-tears-ui:latest`; rolled out to local k8s deployments `lake-of-tears-backend` and `lake-of-tears-lakeui`. Verified live health check at `http://localhost/api/health` and live UI at `http://localhost/settings/admin#services`.

- **Issue #50 & PR #389 (Clarify First-Time Bootstrap Flow & Resolve Day-0 Registration Lockout):**
  - **Status:** **COMPLETED, MERGED & ALL 798 TESTS PASSING**
  - **Pull Request:** Merged via PR #389 into `main` (`8f8749f`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Issue item `PVTI_lADOFEJxWM4Bmf-gzg_5bZY`, PR item `PVTI_lADOFEJxWM4Bmf-gzhGOM20`).
  - **Test Suite:** 798 tests passing (`tests/test_auth_registration_and_providers.py`).
  - **Changes:**
    - Visual indication: Added a Day-0 Bootstrap Callout banner to `/login` when `user_count == 0` explaining that the platform is in Initial Setup Mode and that registering the first account initializes the platform with the root `superadmin` role.
    - Day-0 Lockout Edge Case: Ensured that if `AUTH_REGISTRATION_ENABLED=false` is set in the environment or Helm values, the registration tab and form remain unlocked and visible during initial setup so operators are never locked out of fresh deployments.
    - Auto-Tab Selection: `/login` automatically opens to the "Setup Superadmin" registration tab when 0 users exist rather than displaying a blank sign-in form.
    - Inline hints: Added an alert on the sign-in form if visited while the database is uninitialized with a 1-click action to switch back to superadmin setup.
    - Updated `README.md` and `deploy/helm/lake-of-tears/templates/NOTES.txt` with clear Day-0 bootstrap and Initial Setup instructions.

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

- **Issue #383 & PR #388 (Superadmin Vault Configuration Panel & Workspace Secret Scopes UI):**
  - **Status:** **COMPLETED, DEPLOYED, MERGED, POLISHED & CI GREEN**
  - **Pull Request:** Merged via PR #388 into `main` (`e56c1d2`), with UI reactivity fixes in `282e6fb` and `39f5dba`, and docs in `462c604`.
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Item IDs `PVTI_lADOFEJxWM4Bmf-gzhGI9Nw` and `PVTI_lADOFEJxWM4Bmf-gzhGKyDE`).
  - **Test Suite:** 795 tests passing (`tests/test_ui_secrets.py` and `tests/test_cloud_vault_providers.py`).
  - **Changes:**
    - Superadmin Vault Configuration Panel in `/settings/admin` with tabbed configuration for Internal DB Vault, Azure Key Vault, and AWS Secrets Manager.
    - Live Test Connection AJAX verification with real-time status banners; buttons disabled until valid credentials/endpoints are entered.
    - Cross-workspace secret scopes audit inventory table (`GET /api/admin/vault-providers/scopes`).
    - Workspace Secret Scopes UI in `/settings/workspace` with Databricks-compatible scope listing, provider pills, and user permission badges.
    - Create Scope Modal, Manage Keys slide-over modal with masked write-only secret inputs and visibility toggles, and Scope ACLs Modal for granular READ/WRITE/MANAGE grants.
    - Centered `+ New scope` header button and crisp card styling.
    - **Real-Time Dynamic Reactivity**: Adding, rotating, or deleting keys immediately updates the stored key count in both the slide-over modal drawer and the workspace scope table row behind it with no page refresh needed.
    - **Comprehensive Documentation**: Full User and Admin Guide published at `docs/wiki/Secret-Management.md`, registered in `pages.json` and `Home.md`, and architectural decision record published at `docs/decisions/0003-secret-management-architecture.md`.
  - **Local Deployment:** Rebuilt images `lake-of-tears-backend:latest` and `lake-of-tears-ui:latest`; rolled out to local k8s deployments `lake-of-tears-backend` and `lake-of-tears-lakeui`. Verified live health check at `http://localhost/api/health` and live UI at `http://localhost/settings/workspace#secret-scopes`.

- **Issue #382 & PR #387 (Secret Consumption Runtime, SDK & Automatic Log Redaction):**
  - **Status:** **COMPLETED, DEPLOYED, MERGED & CI GREEN**
  - **Pull Request:** Merged via PR #387 into `main` (`4b0174f`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Item IDs `PVTI_lADOFEJxWM4Bmf-gzhGI8_U` and `PVTI_lADOFEJxWM4Bmf-gzhGKlYQ`).
  - **Test Suite:** 786 tests passing (`tests/test_secret_runtime_and_redaction.py`).
  - **Changes:**
    - Python SDK client in `lake_of_tears.secrets` and `lot.secrets` with Databricks `dbutils.secrets` parity (`secrets.get(scope, key)`, `secrets.list(scope)`, `secrets.list_scopes()`).
    - Config template placeholder interpolator in `backend/secrets_manager/templating.py` supporting `{{secrets/scope/key}}`, `{{secrets.scope.key}}`, and `${secrets:scope:key}` across nested dictionaries, lists, and strings with permission enforcement.
    - Automatic real-time output log redaction in `backend/secrets_manager/redaction.py` with thread-safe secret registry, `SecretRedactionFilter`, and `RedactingStream`.
    - Airflow task instance execution log sanitization in `get_task_log` in `backend/pipeline_service.py`.
  - **Local Deployment:** Rebuilt image `lake-of-tears-backend:latest`; rolled out to local k8s deployment `lake-of-tears-backend` in namespace `lake-of-tears`. Verified live health check at `http://localhost/api/health`.

- **Issue #381 & PR #386 (Cloud Provider Integrations for Azure Key Vault & AWS Secrets Manager):**
  - **Status:** **COMPLETED, DEPLOYED, MERGED & CI GREEN**
  - **Pull Request:** Merged via PR #386 into `main` (`85999e0`).
  - **Kanban Board:** Moved to **Done** on [Lake of Tears (Main) Project #3](https://github.com/orgs/Lake-of-Tears/projects/3) (Item IDs `PVTI_lADOFEJxWM4Bmf-gzhGI8zE` and `PVTI_lADOFEJxWM4Bmf-gzhGKiio`).
  - **Dependencies:** Added `azure-identity>=1.27.0` and `azure-keyvault-secrets>=4.12.0` to `backend/requirements.txt`.
  - **Test Suite:** 782 tests passing (`tests/test_cloud_vault_providers.py`).
  - **Changes:**
    - `AzureKeyVaultProvider` in `backend/secrets_manager/azure_kv.py` supporting DefaultAzureCredential (Managed Identity / Workload Identity) and ClientSecretCredential (Service Principal).
    - `AWSSecretsManagerProvider` in `backend/secrets_manager/aws_sm.py` supporting IAM roles, EKS IRSA, AssumeRole STS, and explicit credentials.
    - Superadmin vault provider REST API in `backend/vault_admin_router.py` mounted at `/api/admin/vault-providers` with provider configuration, credential masking, and live connectivity testing.
    - Dynamic scope dispatch in `backend/secrets_manager/registry.py` merging system-wide provider settings with scope-specific overrides.
  - **Local Deployment:** Rebuilt image `lake-of-tears-backend:latest`; rolled out to local k8s deployment `lake-of-tears-backend` in namespace `lake-of-tears`. Verified live health check at `http://localhost/api/health`.

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


