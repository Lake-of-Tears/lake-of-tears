# Pod and Container SecurityContext Hardening Spec

## Issue & Branch
- **Issue:** [Lake-of-Tears/lake-of-tears#54](https://github.com/Lake-of-Tears/lake-of-tears/issues/54)
- **Branch:** `sec/54-securitycontext-hardening`
- **Milestone:** version 1.0.0
- **Status:** Proposed for Review

---

## 1. Overview & Motivation

A Kubernetes manifest configuration scan with Trivy identified vulnerability **KSV-0118 (High)** across all chart deployments in `deploy/helm/lake-of-tears/templates/`:

> **KSV-0118 (HIGH):** Deployment/Container is using the default security context, which allows root privileges. Relying on default security contexts exposes workloads to attacks leveraging privileged access and fails baseline Kubernetes security policies.

Currently, `backend.yaml`, `ui.yaml`, `postgres.yaml`, `superset.yaml`, `minio.yaml`, and `jupyter.yaml` lack explicit Pod-level and Container-level `securityContext` blocks. Additionally, `airflow.yaml` contains an anti-pattern: pod-level root execution (`runAsUser: 0`) without dropped capabilities or privilege escalation restrictions.

### Objectives
1. **Zero KSV-0118 Warnings:** Eliminate all KSV-0118 findings across all Helm-rendered templates.
2. **Kubernetes Restricted Pod Security Standard (PSS):** Hardened Pod and Container security contexts to adhere to the Kubernetes PSS Restricted profile.
3. **Storage & Permission Integrity:** Align UIDs, GIDs, and `fsGroup` configurations with volume mounts for Postgres, MinIO, Superset, and Airflow so existing data persists cleanly without permission errors.
4. **Declarative Configurability:** Expose granular `podSecurityContext` and `securityContext` settings in `values.yaml` for every workload, allowing operator overrides for non-standard environments (e.g. OpenShift, hardened storage drivers).

---

## 2. Invariants & Security Guardrails

| Invariant | Specification | Enforcement Point |
|-----------|---------------|-------------------|
| **Non-Root Execution** | All pods must enforce non-root execution (`runAsNonRoot: true`) and a positive non-zero UID (`runAsUser > 0`). | Pod `securityContext` |
| **Seccomp Profile** | All pods must enforce modern default syscall filtering (`seccompProfile: { type: RuntimeDefault }`). | Pod `securityContext` |
| **No Privilege Escalation** | All containers and init containers must prohibit privilege elevation (`allowPrivilegeEscalation: false`). | Container `securityContext` |
| **Drop All Capabilities** | All Linux capabilities must be dropped by default (`capabilities: { drop: ["ALL"] }`). | Container `securityContext` |
| **No Hardcoded Root** | No pod or container may set `runAsUser: 0`. Airflow's existing `runAsUser: 0` is strictly removed. | Template & Values |
| **Values Overrideability** | Every workload template must interpolate `.Values.<component>.podSecurityContext` and `.Values.<component>.securityContext` using `toYaml`. | Helm Templates |

---

## 3. Workload Matrix & UID/GID Analysis

Each service utilizes distinct base container images and persistent storage paths. The table below outlines the runtime identities and volume mount permissions for all 9 workloads (7 Deployments and 2 Hook Jobs):

| Workload | Kind | Base Image / Upstream User | Target `runAsUser` : `runAsGroup` | Target `fsGroup` | Storage Volume & Mount Path | Special Considerations |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Lake UI** (`lakeui`) | Deployment | `python:3.12-slim` (root default) | `10001:10001` | `10001` | None | Pure stateless FastAPI shell. Safe unprivileged UID. |
| **Backend** (`backend`) | Deployment | `python:3.12-slim` (root default) | `10001:10001` | `10001` | None | Runs Alembic migrations + FastAPI over HTTP port 8000. Safe unprivileged UID. |
| **PostgreSQL** (`postgres`) | Deployment | `postgres:16-alpine` (user `postgres`: UID 70, GID 70) | `70:70` | `70` | PVC `postgres-data` at `/var/lib/postgresql/data` | Set `PGDATA=/var/lib/postgresql/data/pgdata` to ensure PostgreSQL creates and manages its 0700 data dir under the `fsGroup: 70` volume root. |
| **MinIO** (`minio`) | Deployment | `ghcr.io/coollabsio/minio` (user `minio`: UID 1000 or custom) | `10001:10001` | `10001` | PVC `minio-data` at `/data` | MinIO supports running as arbitrary non-root UID. Volume managed by `fsGroup: 10001`. |
| **MinIO Init** (`minio-init`) | Job | Init: `busybox`<br>App: `ghcr.io/coollabsio/minio` (`mc`) | `10001:10001` | `10001` | None | Set `MC_CONFIG_DIR=/tmp/.mc`. Default `/.mc` fails write permissions when non-root. |
| **Superset** (`superset`) | Deployment | `apache/superset:4.1.4` (user `superset`: UID 1000, GID 1000) | `1000:1000` | `1000` | PVC `superset-home` at `/app/superset_home` | ConfigMap mounted read-only at `/app/pythonpath/superset_config.py`. Volume owned by `fsGroup: 1000`. |
| **Superset Init** (`superset-init`) | Job | Init: `busybox`<br>App: `apache/superset:4.1.4` | `1000:1000` | `1000` | PVC `superset-home` at `/app/superset_home` | Runs `superset db upgrade` and `fab create-admin`. Shares UID 1000 and `fsGroup: 1000`. |
| **Airflow** (`airflow`) | Deployment | `lake-of-tears-airflow` (`apache/airflow:2.10.3`, UID 50000, GID 0) | `50000:0` | `50000` | PVC `airflow-home` at `/opt/airflow` | Remove hardcoded `runAsUser: 0`. Airflow installs `_PIP_ADDITIONAL_REQUIREMENTS` into user space (`~/.local`) without root. |
| **JupyterLab** (`jupyter`) | Deployment | `jupyter/datascience-notebook` (user `jovyan`: UID 1000, GID 100) | `1000:100` | `100` | None (or local emptyDir) | Jovyan user installs user packages into `/opt/conda` and runs notebook on port 8888. |

---

## 4. Proposed Configuration Changes

### 4.1. `deploy/helm/lake-of-tears/values.yaml`

Add hardened defaults under each service block:

```yaml
# ===========================================================================
# MinIO
# ===========================================================================
minio:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

# ===========================================================================
# Lake UI
# ===========================================================================
lakeui:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

# ===========================================================================
# JupyterLab
# ===========================================================================
jupyter:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 100
    fsGroup: 100
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

# ===========================================================================
# Apache Superset
# ===========================================================================
superset:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

# ===========================================================================
# Apache Airflow
# ===========================================================================
airflow:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 50000
    runAsGroup: 0
    fsGroup: 50000
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

# ===========================================================================
# Auth Backend
# ===========================================================================
backend:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

# ===========================================================================
# PostgreSQL
# ===========================================================================
postgres:
  # ... existing fields ...
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 70
    runAsGroup: 70
    fsGroup: 70
    seccompProfile:
      type: RuntimeDefault
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
```

---

## 5. Proposed Template Adjustments

### 5.1. `backend.yaml` and `ui.yaml`
- In `spec.template.spec`:
  ```yaml
  securityContext:
    {{- toYaml .Values.backend.podSecurityContext | nindent 8 }}
  ```
- In `containers[0]`:
  ```yaml
  securityContext:
    {{- toYaml .Values.backend.securityContext | nindent 12 }}
  ```

### 5.2. `postgres.yaml`
- In `spec.template.spec`:
  ```yaml
  securityContext:
    {{- toYaml .Values.postgres.podSecurityContext | nindent 8 }}
  ```
- In `containers[0]`:
  ```yaml
  securityContext:
    {{- toYaml .Values.postgres.securityContext | nindent 12 }}
  ```
- In `containers[0].env`:
  ```yaml
  - name: PGDATA
    value: /var/lib/postgresql/data/pgdata
  ```
  *Rationale:* When mounting Persistent Volumes in Kubernetes, the mount point root (`/var/lib/postgresql/data`) is governed by root/fsGroup filesystem permissions (often 0775 or 0755). PostgreSQL `initdb` strictly mandates 0700 permissions. Placing cluster data in a subpath (`pgdata`) allows the unprivileged `postgres` process to initialize the directory with `0700` without permission denied errors.

### 5.3. `minio.yaml`
- In Deployment `spec.template.spec`:
  ```yaml
  securityContext:
    {{- toYaml .Values.minio.podSecurityContext | nindent 8 }}
  ```
- In Deployment container:
  ```yaml
  securityContext:
    {{- toYaml .Values.minio.securityContext | nindent 12 }}
  ```
- In `minio-init` Job:
  - Add pod-level security context:
    ```yaml
    securityContext:
      {{- toYaml .Values.minio.podSecurityContext | nindent 8 }}
    ```
  - Add container-level security context to both `wait-for-minio` and `mc`:
    ```yaml
    securityContext:
      {{- toYaml .Values.minio.securityContext | nindent 12 }}
    ```
  - In `mc` container `env`, add:
    ```yaml
    - name: MC_CONFIG_DIR
      value: /tmp/.mc
    ```
    *Rationale:* `mc` writes its configuration/aliases to `/.mc` by default, which fails with permission denied when running as non-root. `/tmp/.mc` provides a safe, writable location.

### 5.4. `superset.yaml`
- In Deployment `spec.template.spec`:
  ```yaml
  securityContext:
    {{- toYaml .Values.superset.podSecurityContext | nindent 8 }}
  ```
- In Deployment container:
  ```yaml
  securityContext:
    {{- toYaml .Values.superset.securityContext | nindent 12 }}
  ```
- In `superset-init` Job:
  - Add pod-level security context:
    ```yaml
    securityContext:
      {{- toYaml .Values.superset.podSecurityContext | nindent 8 }}
    ```
  - Add container-level security context to both `wait-for-superset` and `superset-init`:
    ```yaml
    securityContext:
      {{- toYaml .Values.superset.securityContext | nindent 12 }}
    ```

### 5.5. `airflow.yaml`
- Remove hardcoded `runAsUser: 0` from line 24.
- In `spec.template.spec`:
  ```yaml
  securityContext:
    {{- toYaml .Values.airflow.podSecurityContext | nindent 8 }}
  ```
- In container:
  ```yaml
  securityContext:
    {{- toYaml .Values.airflow.securityContext | nindent 12 }}
  ```

### 5.6. `jupyter.yaml`
- In `spec.template.spec`:
  ```yaml
  securityContext:
    {{- toYaml .Values.jupyter.podSecurityContext | nindent 8 }}
  ```
- In container:
  ```yaml
  securityContext:
    {{- toYaml .Values.jupyter.securityContext | nindent 12 }}
  ```

---

## 6. Migration & Compatibility Considerations

### 6.1. Existing Persistent Volumes & Storage Providers
When updating a running cluster:
1. **Kubernetes `fsGroup` Behavior:**
   - When `fsGroup` is defined, Kubernetes automatically recursively updates permissions of mounted volumes to match `fsGroup` (via `chown`/`chmod`), ensuring the container's GID can write to existing files.
   - For CSI drivers or NFS mounts that do not support automatic recursive chown, the operator can configure `fsGroupChangePolicy: "OnRootMismatch"` if supported, or manually align permissions.
2. **PostgreSQL Data Migration:**
   - If an existing Postgres volume was created with data located directly in `/var/lib/postgresql/data` (without `pgdata` subdirectory), the official Postgres entrypoint checks if `/var/lib/postgresql/data/PG_VERSION` exists.
   - To maintain 100% backward compatibility for existing deployments while hardening new installs:
     - The entrypoint detects existing databases. If `PG_VERSION` is at root, `docker-entrypoint.sh` continues to read it; otherwise it initializes in `$PGDATA`.
     - Alternatively, we provide a pre-upgrade migration note if volume restructuring is required.

---

## 7. Verification & Acceptance Criteria

### Acceptance Gate 1: Helm Lint & Render
1. `helm lint deploy/helm/lake-of-tears --namespace lake-of-tears` exits 0 with 0 errors.
2. Template rendering succeeds across all sample value files:
   - `deploy/helm/values-local.example.yaml`
   - `deploy/helm/values-prod.yaml`

### Acceptance Gate 2: Zero Trivy KSV-0118 Warnings
Render the manifests and execute Trivy configuration scanner:
```bash
helm template lake-of-tears deploy/helm/lake-of-tears \
  --namespace lake-of-tears \
  -f deploy/helm/values-local.example.yaml > /tmp/rendered.yaml

trivy config /tmp/rendered.yaml
```
**Assertion:**
- **0** warnings matching `KSV-0118` across all Deployments and Jobs.

### Acceptance Gate 3: Kubernetes Restricted Pod Security Standard
Validate rendered manifests against Kubernetes Pod Security Admission:
- All pods specify:
  - `runAsNonRoot: true`
  - `seccompProfile.type: RuntimeDefault`
  - `allowPrivilegeEscalation: false`
  - `capabilities.drop: ["ALL"]`
- No pod runs with UID 0.

### Acceptance Gate 4: CI Integration
Add a dedicated step to `.github/workflows/ci.yml` under `helm-lint`:
```yaml
      - name: Security Scan Helm Templates
        run: |
          helm template test deploy/helm/lake-of-tears \
            --namespace lake-of-tears \
            -f deploy/helm/values-local.example.yaml > /tmp/rendered.yaml
          trivy config /tmp/rendered.yaml --exit-code 1 --severity HIGH,CRITICAL --include-non-failures=false
```

---

## 8. Decisions & Rationale

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Per-service securityContext in `values.yaml`** | Per-service rather than single global object | Workloads run under different UIDs (`70` for Postgres, `1000` for Superset/Jupyter, `50000` for Airflow, `10001` for UI/Backend/MinIO). Per-service configuration reflects reality and avoids leaky abstractions. |
| **Remove Airflow `runAsUser: 0`** | Run as `50000:0` with `fsGroup: 50000` | The existing comment claimed root was required for pip startup installs. Official Airflow images install into user site-packages (`~/.local`) without root. Removing root is required for PSS Restricted compliance. |
| **MinIO Client `MC_CONFIG_DIR`** | `/tmp/.mc` | MinIO Client (`mc`) defaults to writing aliases to `/.mc`, which fails for unprivileged users. Explicitly setting `MC_CONFIG_DIR=/tmp/.mc` guarantees reliable hook execution. |
| **PostgreSQL `PGDATA` subpath** | `/var/lib/postgresql/data/pgdata` | K8s PVC mount points cannot always be `chmod 0700` by unprivileged users. Using a subdirectory ensures `initdb` creates a private 0700 folder without mount-point collision. |
