# 0004: Helm Lifecycle Management Suite and TOON-Driven Operations

- **Status:** Accepted and implemented via ticket #296 (PR #391).
- **Tickets:** #296 (Helm Lifecycle Scripts: install, update, and destroy with TOON configuration).
- **Date:** 2026-11-05

## Context

Prior to this architecture, provisioning, upgrading, and tearing down Lake of Tears on Kubernetes required manual multi-step `helm` and `kubectl` invocations. While PR #325 introduced Token-Oriented Object Notation (TOON) as the canonical configuration standard, operators still faced several operational hurdles:
1. **Initial Setup Friction & Secrets:** Copying templates required manual editing of placeholder credentials (`changeme-*`), with no automated secret generation or validation.
2. **Platform Updates & Drift:** There was no automated mechanism to check if a deployed cluster was behind current releases, nor was there a mechanism to safely perform rolling updates with automatic rollback.
3. **Data Loss Risks during Teardown:** Running manual uninstalls risked accidental deletion of PersistentVolumeClaims (PVCs) containing databases, object storage, and user notebooks.
4. **Future Admin Panel Integration:** Lake of Tears planned to introduce one-click in-app update checks and rollout controls inside the Lake UI Admin Panel (modeled after TrueNAS SCALE update management), which requires a unified underlying CLI and API engine.

To solve these challenges, Lake of Tears implemented a unified **Helm Lifecycle Management Suite** (`./lot` and `deploy/scripts/`).

---

## Decision 1: Strict Alignment with TOON over Legacy TOML

**Chosen: Standardize lifecycle automation around Token-Oriented Object Notation (`lake-of-tears.toon`) and dynamic Helm compilation via `scripts/toon2helm.py`.**

Issue #296 was initially drafted proposing a static TOML file (`lot-config.toml`) parsed with `tomllib`. However, Lake of Tears had already standardized on TOON across the entire repository to reduce configuration token counts by 40–60% for LLM DevOps agents and human operators.

| | Chosen: Native TOON Lifecycle | Alternative: Introduce TOML Configuration |
|---|---|---|
| Idea | Drive `install.sh`, `update.sh`, and `destroy.sh` directly from `lake-of-tears.toon` via `toon2helm.py` | Add a parallel `lot-config.toml` specification alongside existing `.toon` files |
| For | Single source of truth; zero configuration drift; leverages established in-memory streaming to Helm; zero plaintext secrets on disk | Uses standard library `tomllib` in Python 3.11+ |
| Against | Requires Python PyYAML dependency for YAML serialization | Creates duplicate conflicting configuration standards across the repository |
| To revert | Add a TOML parser and rewrite `deploy/scripts/common.sh` to read TOML files. | n/a |

---

## Decision 2: Dual-Layer Interface (Modular Scripts + Root `./lot` CLI)

**Chosen: Dedicated executable scripts in `deploy/scripts/` combined with a unified root CLI runner `./lot`.**

To support both interactive operators and headless CI/CD automation:
- **`./lot <command>`**: Provides an intuitive command center (`install`, `update`, `destroy`, `status`, `config`).
- **`deploy/scripts/{install,update,destroy,status}.sh`**: Standalone modular scripts with exit codes, non-interactive flags (`--yes`, `--force`, `--json`, `--auto-generate-secrets`), and strict POSIX/Bash compatibility.

| | Chosen: Dual-Layer Interface (`./lot` + `deploy/scripts/`) | Alternative: Monolithic Shell Script |
|---|---|---|
| Idea | Root CLI runner delegating to discrete single-purpose scripts | Single 1,000+ line script containing all operations and subcommands |
| For | Clean separation of concerns; individual scripts can be isolated and executed directly by CI runners or Kubernetes jobs | Only one script file in repository |
| Against | Slight indirection through CLI router | Difficult to test and maintain; higher risk of regression across commands |
| To revert | Merge scripts into a single file under `deploy/scripts/lot.sh`. | n/a |

---

## Decision 3: TrueNAS-Style Update Architecture with Trains & Machine-Readable Output

**Chosen: Implement update trains (`stable` and `edge`), `--check-only`, and structured `--json` output modeled after TrueNAS SCALE middleware.**

To prepare for direct integration into the Lake of Tears Admin Panel (`/settings/admin#updates`):
1. **Update Trains:**
   - `stable`: Tracks official GitHub release build tags (`v1.x.x`).
   - `edge`: Tracks the latest commits on `origin/main`.
2. **Inspection & Telemetry:**
   - The `--check-only --json` flag returns structured JSON containing status (`AVAILABLE` vs `UNAVAILABLE`), current version, latest version, changelog lines, and update eligibility.
   - The Lake UI FastAPI backend can query this engine directly or run it inside an in-cluster Kubernetes job.
3. **Atomic Rollback Safety:**
   - Upgrades execute with `helm upgrade --atomic --timeout 10m`. If pods fail health or readiness checks, Helm automatically rolls back to the prior working revision with zero manual intervention.

---

## Decision 4: Safe-by-Default Persistent Storage Retention

**Chosen: Teardown preserves PersistentVolumeClaims by default, requiring explicit typing confirmation of the namespace name (`lake-of-tears`) for destructive purges.**

When `helm uninstall` runs, Kubernetes does not automatically delete PVCs. `deploy/scripts/destroy.sh` enforces this safe behavior:
- Default / `--keep-data`: Uninstalls the Helm release while leaving MinIO, PostgreSQL, Airflow, and Jupyter PVCs intact in the namespace for future reattachment.
- `--purge-data`: Permanently terminates all PVCs and the namespace. In interactive mode, operators must explicitly type the namespace name (`lake-of-tears`) to confirm data destruction (bypassed with `--force` in automated CI teardowns).
