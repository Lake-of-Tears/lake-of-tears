# Deployment and Operations

This is the operational runbook for Lake of Tears: setting up a local environment with Docker Compose, deploying to Kubernetes with Helm, checking that everything is healthy, keeping it maintained, and rotating secrets. Troubleshooting steps are collected in section 6.

The Docker Compose, backup, restore, health-check and credential-rotation procedures here were run against a local stack. The Helm material was checked by linting and rendering the chart (`helm lint`, `helm template`, `helm install --dry-run=client`); it was **not** installed on a cluster, and each place that depends on cluster behavior says so.

## 1. Choose a deployment model

| | Docker Compose | Kubernetes with Helm |
|---|---|---|
| Use it for | Evaluation, development, one machine | Production and multi-user installs |
| Entry point | nginx container on port 80 | `ingress-nginx` Ingress with your hostnames |
| Configuration | `.env` file | `values.yaml` plus override files |
| Storage | Docker volumes and host folders | PersistentVolumeClaims |
| Start | `docker compose up -d` | `helm upgrade --install ...` |

## 2. Local setup with Docker Compose

### Prerequisites

- Docker with Compose v2. The full stack (8 services) ran in a Docker VM with 8 GB of memory; give Docker at least that.
- Ports 80, 3000, 8000, 8080, 8088, 9000 and 9001 free on the host. Port 80 is the one you will use.

### Steps

```bash
git clone https://github.com/Lake-of-Tears/lake-of-tears.git
cd lake-of-tears
cp .env.example .env
```

Edit `.env`. At minimum set strong values for these (the rest have usable defaults):

| Variable | What it is |
|---|---|
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | MinIO root account (5 to 20 letters and digits; password 8 or more characters). Also set `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` to the same values |
| `AUTH_SECRET_KEY` | Signs user sessions (JWTs); generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `POSTGRES_AUTH_PASSWORD` | Password of the `lake_auth` database |
| `JUPYTER_TOKEN` | Token for the Jupyter server |
| `SUPERSET_SECRET_KEY`, `SUPERSET_ADMIN_PASSWORD` | Superset |
| `AIRFLOW_SECRET_KEY`, `AIRFLOW_PASSWORD` | Airflow |
| `DATALAKE_DATA_DIR` | A host folder for notebooks, Airflow DAGs and pipeline code. It defaults to `~`; point it at a dedicated folder |

The Gemini API key is not in `.env`: a superadmin adds it later in **Settings, Admin Panel**.

Create the host folders that the containers mount (otherwise Docker creates them owned by root):

```bash
mkdir -p "$DATALAKE_DATA_DIR"/{airflow/dags,notebooks,datalake}
docker compose up -d
```

Open **http://localhost** and register the first account: it becomes the superadmin. Use port 80 for everything: the direct UI port (3000) does not proxy `/api/`, so signing in fails there.

### Services and ports

| Service | Container | Reached at | Published port |
|---|---|---|---|
| nginx (entry point) | `nginx` | http://localhost | 80 |
| Lake UI | `lake-ui` | through nginx | 3000 (debugging only) |
| Backend API | `backend` | http://localhost/api/ | 8000 |
| PostgreSQL | `postgres` | internal | none |
| MinIO S3 API and console | `minio` | internal / console | 9000 (S3), 9001 (console) |
| Superset | `superset` | http://localhost/superset/ | 8088 |
| Airflow | `airflow` | http://localhost/airflow/ | 8080 |
| JupyterLab | `jupyter` | through the Lake UI only | none |

`minio-init` and `superset-init` are one-shot containers that create the bucket and the Superset admin; they exit when done.

### Everyday commands

```bash
docker compose ps                    # status and health
docker compose logs -f backend       # follow one service's logs
docker compose restart airflow       # restart one service
docker compose up -d --build lake-ui backend   # rebuild after code changes
docker compose down                  # stop and remove containers (data volumes are kept)
```

`docker compose down -v` also **deletes the data volumes** (MinIO, PostgreSQL, Superset). Do not use `-v` unless you want to start from nothing.

> After a container is recreated (for example by `up -d --build`), nginx may keep the old address and answer **502**. Run `docker compose restart nginx`.

## 3. Production on Kubernetes with Helm

### Prerequisites

Kubernetes with an `ingress-nginx` controller, a StorageClass for PersistentVolumeClaims, DNS names for your hostnames, and `helm` 3 or later with `kubectl`.

### Chart structure

`deploy/helm/lake-of-tears/` contains `Chart.yaml`, `values.yaml` and these templates:

| Template | Creates |
|---|---|
| `backend.yaml`, `ui.yaml` | Backend API and Lake UI deployments and services |
| `postgres.yaml` | PostgreSQL |
| `minio.yaml` | MinIO deployment, services and the bucket-creation job |
| `airflow.yaml` | Airflow (`LocalExecutor`, PostgreSQL metadata) |
| `superset.yaml`, `superset-config.yaml` | Superset, its config and an init job |
| `jupyter.yaml` | JupyterLab |
| `ingress.yaml` | The Ingress resources |
| `pvc.yaml` | PersistentVolumeClaims |
| `secrets.yaml` | The Secret, built from your values |
| `validation.yaml` | Install-time guards (see below) |
| `NOTES.txt` | URLs printed after install |

A default install renders 7 deployments, 2 one-shot jobs, 6 Ingress resources and 4 PVCs.

### Values files

| File | Use |
|---|---|
| `deploy/helm/lake-of-tears/values.yaml` | Defaults and documentation of every setting |
| `deploy/helm/values-local.yaml` | A local cluster (OrbStack, Docker Desktop, kind, k3s): `*.localhost` hosts, `local-path` storage, development credentials |
| `deploy/helm/values-local.example.yaml` | A template to copy and customise |
| `deploy/helm/values-prod.yaml` | An example of production overrides (a single-VM k3s install in this repository) |
| `/etc/lake-of-tears/values-secret.yaml` | **Your** secrets, kept outside the repository and never committed |

Pass several files with repeated `--values`; later files win.

### Namespace guard and required secrets

The chart refuses to install into the `default` namespace:

```
[FATAL ERROR] Lake of Tears cannot be deployed into the 'default' namespace.
```

Always use `--namespace lake-of-tears --create-namespace`. It can be bypassed with `--set global.allowDefaultNamespace=true`, but you should not.

Without secrets, rendering fails with the first missing value (for example `minio.rootPassword is required`). The required values are:

```yaml
# /etc/lake-of-tears/values-secret.yaml
minio:    { rootUser: "minio", rootPassword: "<strong password>" }
jupyter:  { token: "<random token>" }
superset: { secretKey: "<random>", adminPassword: "<password>" }
airflow:  { secretKey: "<random>", adminPassword: "<password>" }
postgres:
  auth:    { password: "<password>" }     # lake_auth database
  airflow: { password: "<password>" }    # Airflow metadata database
backend:
  auth: { secretKey: "<random>" }         # JWT signing key
```

### Security context policy

Rendering the chart shows every workload hardened the same way:

- **Pod level:** `runAsNonRoot: true`, a fixed non-root `runAsUser`, and `seccompProfile: RuntimeDefault`. Users differ per image (backend and UI 10001, MinIO 10001, PostgreSQL 70, Airflow 50000, Jupyter 1000, Superset 1000).
- **Container level (including init containers and jobs):** `allowPrivilegeEscalation: false` and `capabilities: drop: [ALL]`.

These can be overridden per service through `<service>.securityContext` in values.

### Ingress and TLS

Hostnames come from the `ingress` block:

| Setting | Serves |
|---|---|
| `ingress.host` | Lake UI, the API and `/airflow` |
| `ingress.supersetHost` | Superset (its own host, because path-based proxying breaks its assets) |
| `ingress.minioConsoleHost` | MinIO console |
| `ingress.minioApiHost` | MinIO S3 API, for external clients |

Enable TLS by listing a secret and the hosts. The secret must already exist in the namespace (for example created by cert-manager):

```yaml
ingress:
  tls:
    - secretName: lake-of-tears-tls
      hosts:
        - lake.example.com
        - superset.example.com
        - minio.example.com
        - minio-api.example.com
```

With `tls` set, the rendered Ingress resources carry the `tls` section. Also set `backend.auth.baseUrl` and `lakeui.minioConsoleUrl` to the `https://` addresses so OAuth callbacks and the Storage page links are correct.

### Storage

The chart creates PVCs for PostgreSQL, MinIO data, Superset home and Airflow home, all `ReadWriteOnce`, using `global.storageClass` (empty means the cluster default). Sizes are set per service with `<service>.persistence.size`; the defaults are 200 Gi for MinIO, 5 Gi for PostgreSQL and 1 Gi for Superset and Airflow, so size MinIO to your data.

> **Known gap:** `jupyter.persistence.size` exists in the values but is not used by any template, so Jupyter has no persistent volume and notebooks are lost if its pod restarts ([#176](https://github.com/Lake-of-Tears/lake-of-tears/issues/176)).

The PVCs carry no `helm.sh/resource-policy: keep` annotation, so `helm uninstall` removes them and **their data** (expected Helm behavior; not tested on a cluster). Back up before uninstalling.

### Install, upgrade and roll back

```bash
helm upgrade --install lake-of-tears ./deploy/helm/lake-of-tears \
  --namespace lake-of-tears --create-namespace \
  --values deploy/helm/values-prod.yaml \
  --values /etc/lake-of-tears/values-secret.yaml \
  --wait --timeout 10m

helm lint ./deploy/helm/lake-of-tears --namespace lake-of-tears    # before upgrading
helm history lake-of-tears -n lake-of-tears
helm rollback lake-of-tears <revision> -n lake-of-tears
```

Check what will be applied without a cluster with `helm template ... ` or `helm install ... --dry-run=client`, which also prints the post-install notes. The Jupyter URL appears in the notes only if you enabled the opt-in `jupyter.ingress`.

## 4. Monitoring

### Health endpoints

| Service | Check | Healthy answer |
|---|---|---|
| Lake UI | `GET /health` (port 3000, or through nginx at `/health`) | `{"status":"ok"}` |
| Backend API | `GET /api/health` | `{"status":"ok"}` |
| MinIO | `GET :9000/minio/health/live` and `/minio/health/ready` | `200` |
| Superset | `GET :8088/health` | `OK` |
| Airflow | `GET /airflow/health` (JSON with scheduler and metadatabase status) | `200` |
| PostgreSQL | `pg_isready -U lake_auth -d lake_auth` | accepting connections |

We called all of these on the running stack and got `200`, except `/airflow/health` through nginx, which currently returns 404 because of a configuration bug ([#163](https://github.com/Lake-of-Tears/lake-of-tears/issues/163)); `http://localhost:8080/airflow/health` answers directly.

Compose uses these in container health checks (PostgreSQL, MinIO, Jupyter, Superset), so `docker compose ps` shows `healthy`. In Kubernetes the chart defines liveness and readiness probes on the same endpoints for the backend, UI, MinIO, Superset, Airflow and PostgreSQL.

Quick checks:

```bash
docker compose ps
for u in http://localhost/health http://localhost/api/health http://localhost:9000/minio/health/live; do
  printf '%s -> ' "$u"; curl -s -o /dev/null -w '%{http_code}\n' "$u"
done
kubectl get pods,pvc,ingress -n lake-of-tears           # Kubernetes
kubectl logs deploy/lake-of-tears-backend -n lake-of-tears
```

Look at Airflow's task logs and DAG state for pipeline problems ([Airflow Orchestration](Airflow-Orchestration.md)).

## 5. Maintenance

### Upgrading

- **Compose:** `git pull`, then `docker compose up -d --build`. Database migrations (Alembic) run automatically when the backend starts. Restart nginx afterwards if you see 502 (section 2).
- **Helm:** `helm upgrade` as above. Take a backup first (below); `helm rollback` restores the previous chart revision but **not** the database schema, so keep a database dump from before any upgrade.

### Backups

What to back up for Docker Compose:

| Data | Where | How |
|---|---|---|
| Users, workspaces, catalogs (PostgreSQL) | volume `postgres_data` | `pg_dump` (below) |
| All data files (MinIO) | volume `minio_data` | volume archive (below), or `mc mirror` |
| Notebooks, DAGs, pipeline code, Airflow database | `$DATALAKE_DATA_DIR` | copy the folder |
| Superset dashboards | volume `superset_home` | volume archive |
| Your configuration | `.env` | copy it somewhere safe; it contains secrets |

PostgreSQL dump and restore. We dumped the live database, restored it into a scratch database and compared row counts (they matched, with no errors):

```bash
docker compose exec -T postgres pg_dump -U lake_auth -d lake_auth > lake_auth_$(date +%F).sql

# test a restore without touching the live database
docker compose exec -T postgres psql -U lake_auth -d postgres -c "CREATE DATABASE restore_check"
docker compose exec -T postgres psql -U lake_auth -d restore_check < lake_auth_2026-10-17.sql
docker compose exec -T postgres psql -U lake_auth -d postgres -c "DROP DATABASE restore_check"
```

MinIO data volume archive (the volume name is prefixed with the Compose project name):

```bash
docker run --rm -v lake-of-tears_minio_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/minio_data_$(date +%F).tgz -C /data .
```

For a cluster, take PVC snapshots with your storage provider, or use `pg_dump` through `kubectl exec` and `mc mirror` for MinIO.

### Storage lifecycle

- **Application cleanup.** Deleted catalogs and workspaces are purged after a grace period by the `purge_deleted_catalogs` (daily 02:00) and `purge_inactive_workspaces` (hourly) DAGs. The grace periods are set by a superadmin in the Admin Panel; see [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md).
- **MinIO lifecycle rules.** To expire objects automatically use MinIO's `mc ilm`. Add a rule only for a prefix you own, for example scratch output; do not expire `raw/` or catalog prefixes by accident. We added, listed and removed a rule on a test bucket:

  ```bash
  mc alias set lake http://localhost:9000 <access key> <secret key>
  mc ilm rule add --expire-days 30 --prefix "tmp/" lake/datalake
  mc ilm rule ls lake/datalake
  mc ilm rule rm --all --force lake/datalake   # removes every rule on the bucket; use --id to remove one
  ```

- **Small files.** Many small Parquet files slow queries; automatic compaction is a planned improvement ([#103](https://github.com/Lake-of-Tears/lake-of-tears/issues/103)).
- **Disk usage.** `docker system df` for Docker, the MinIO console (port 9001) for bucket sizes, and `kubectl get pvc` for volume sizes.

### Rotating secrets

Rotate on a schedule and whenever a secret may have been exposed. Edit `.env` (Compose) or your values-secret file (Helm), then restart the affected services. For Kubernetes the chart does not restart pods when a Secret changes, so run `kubectl rollout restart deployment -n lake-of-tears` after `helm upgrade`.

| Secret | What to do | Effect |
|---|---|---|
| `AUTH_SECRET_KEY` | Set a new value; `docker compose up -d` (it is shared by the backend, UI and Airflow) | All users are signed out (existing tokens no longer validate) |
| `POSTGRES_AUTH_PASSWORD` | **First** change it inside PostgreSQL: `docker compose exec postgres psql -U lake_auth -d lake_auth -c "ALTER USER lake_auth PASSWORD '<new>'"`, **then** update `.env` and `docker compose up -d backend` | None for users. Changing only `.env` does **not** change the database password: we tested that the old password kept working and the new one was rejected until `ALTER USER` ran |
| `MINIO_ROOT_PASSWORD` and `MINIO_SECRET_KEY` | Set both to the same new value; `docker compose up -d` | MinIO applies the new root password on restart (tested: the old one was rejected, the new one accepted); services reconnect when recreated |
| `JUPYTER_TOKEN` | Update `.env`; `docker compose up -d` (Jupyter, UI, backend and Airflow all use it) | Jupyter restarts, so running notebook kernels stop |
| `AIRFLOW_PASSWORD` | Update `.env`; `docker compose up -d airflow` | Applied on every start once [#165](https://github.com/Lake-of-Tears/lake-of-tears/issues/165) is merged; until then Compose Airflow uses a generated password in `$DATALAKE_DATA_DIR/airflow/standalone_admin_password.txt` |
| `AIRFLOW_SECRET_KEY` | Update `.env`; restart Airflow | Airflow web sessions end |
| `SUPERSET_SECRET_KEY` | Update `.env`; restart Superset | Sessions end. Superset also encrypts stored database passwords with this key, so follow Superset's `superset re-encrypt-secrets` procedure when changing it (not tested here) |
| Gemini API key | Superadmin: Settings, Admin Panel | Stored encrypted; takes effect immediately, no restart |
| SSO client secrets | Rotate at the provider, update `.env`, restart the backend | Users re-authenticate |

After any rotation, run the health checks in section 4 and sign in once.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Bind for 0.0.0.0:80 failed: port is already allocated` | Another program uses port 80. Stop it, or change the nginx port mapping in `docker-compose.yml` |
| Sign-in shows "Not Found" | You opened `http://localhost:3000`. Use `http://localhost` |
| **502 Bad Gateway** from `http://localhost` after a rebuild | nginx has the old container address: `docker compose restart nginx` |
| `http://localhost/airflow/` returns 502 or 404 | A configuration bug being fixed in [#163](https://github.com/Lake-of-Tears/lake-of-tears/issues/163); check `docker compose logs airflow` for `trailing slash` |
| Airflow UI is down after `docker compose restart airflow` | A stale `airflow-webserver.pid`; fixed in the entrypoint by [#165](https://github.com/Lake-of-Tears/lake-of-tears/issues/165). Remove `/opt/airflow/airflow-webserver.pid` and restart |
| Airflow REST API or the Lake UI pipeline pages return 401 | The Compose Airflow password is generated, not `AIRFLOW_PASSWORD` ([#165](https://github.com/Lake-of-Tears/lake-of-tears/issues/165)) |
| Containers cannot write to `$DATALAKE_DATA_DIR` | The folder does not exist or has the wrong owner. Create `airflow/dags`, `notebooks` and `datalake` first |
| Services restart in a loop or are killed | Not enough memory. Give Docker at least 8 GB; check `docker compose logs <service>` |
| `helm install` stops with `cannot be deployed into the 'default' namespace` | Add `--namespace lake-of-tears --create-namespace` |
| `helm install` stops with `... is required` | A secret is missing from your values-secret file (section 3) |
| Pods stay `Pending` | No usable StorageClass: set `global.storageClass` and check `kubectl get pvc -n lake-of-tears` |
| Notebooks disappear after a Jupyter pod restart (Kubernetes) | Known gap, [#176](https://github.com/Lake-of-Tears/lake-of-tears/issues/176) |
| Changed a secret but nothing changed (Kubernetes) | Pods read secrets at start; `kubectl rollout restart deployment -n lake-of-tears` |
| Changed `POSTGRES_AUTH_PASSWORD` and the backend cannot connect | The database password was not changed; run `ALTER USER` (section 5) |

## Related pages

- [Home](Home.md)
- [Architecture Overview](Architecture-Overview.md): what each component does
- [Airflow Orchestration](Airflow-Orchestration.md): pipeline operations
- [DuckDB Querying and Analytics](DuckDB-Querying-and-Analytics.md)
- [Workspaces and Catalog Management](Workspaces-and-Catalog-Management.md): purge DAGs and grace periods
