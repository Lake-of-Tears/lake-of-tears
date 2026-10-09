# Lake of Tears 💧

**The open source, self-hosted Databricks alternative — datalakehouse with AI-powered search, unified web shell, and one-command deployment**

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Docker Compose: ready](https://img.shields.io/badge/Docker%20Compose-ready-2496ED?logo=docker&logoColor=white)
![Helm: ready](https://img.shields.io/badge/Helm-ready-0F1689?logo=helm&logoColor=white)
![Python: 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2?logo=discord&logoColor=white)](https://thelakeoftears.com)
[![Website](https://img.shields.io/badge/Website-thelakeoftears.com-00B4D8)](https://thelakeoftears.com)

> Screenshot: Lake of Tears unified shell

---

## What is Lake of Tears?

Lake of Tears is a self-hosted data platform inspired by Databricks — one URL, one sidebar, all your data tools in one place. It combines S3-compatible storage, a DuckDB SQL engine, AI-powered natural language search, automated pipelines, and business BI — without the enterprise price tag.

**Everything runs behind a single nginx entry point.** JupyterLab, Apache Superset, and Apache Airflow are embedded as iframes within the Lake UI shell, so your team works from one consistent interface.

---

## Features

- **Unified UI shell** — Databricks-inspired sidebar with Notebooks, SQL Editor, Dashboards, Pipelines, AI Query, and Data Catalog in one interface
- **Single entry point** — nginx proxy at port 80; no juggling separate ports or tabs
- **Authentication** — email/password login, account self-registration, and SSO via Google, GitHub, Microsoft, or any generic OIDC provider (Okta, Keycloak, Auth0, etc.)
- **Multi-user with roles** — `admin` and `viewer` roles; first registered user becomes admin automatically
- **Theme toggle** — Light / Dark / Auto (follows OS preference, falls back to time-of-day)
- S3-compatible object storage via MinIO CE
- Parquet-native storage partitioned by `year=/month=/day=`
- DuckDB query engine — run SQL directly over S3, no separate query server required
- 768-dim vector embeddings via Gemini + DuckDB VSS extension for semantic search
- RAG queries: ask questions in plain English, get AI answers grounded in your data
- Anomaly detection via scikit-learn Isolation Forest + Gemini explanations
- Built-in pipeline orchestration via Apache Airflow (embedded)
- Interactive notebooks via JupyterLab (embedded)
- BI dashboards via Apache Superset (embedded)
- Deploy anywhere: Docker Compose (single node) or Kubernetes (Helm)

---

## Architecture

```
                    http://localhost (port 80)
                           │
                    ┌──────▼──────┐
                    │    nginx    │  ← single entry point
                    └──────┬──────┘
       ┌────────┬──────────┼───────┬──────────┐
       ▼        ▼          ▼       ▼          ▼
   /api/    Lake UI  /jupyter/ /superset/ /airflow/
  Backend   (shell)  JupyterLab Superset   Airflow
  (auth)                                   MinIO API :9000

┌──────────────┐    ┌──────────────────────────────────────┐
│  Data Sources│    │           Lake of Tears               │
│              │    │                                        │
│  Stripe      ├───►│  Airflow  ──►  MinIO CE (S3)          │
│  Shopify     ├───►│  Pipelines    s3://datalake/raw/       │
│  HubSpot     ├───►│               s3://datalake/embeddings/│
│  PostgreSQL  ├───►│                     │                  │
└──────────────┘    │               DuckDB │ VSS             │
                    │                     ▼                  │
                    │  Gemini API ──► Embeddings             │
                    │                     │                  │
                    │     ┌───────────────▼──────────────┐   │
                    │     │        Lake UI Shell          │   │
                    │     │  Home · Catalog · Storage     │   │
                    │     │  SQL Editor · AI Query        │   │
                    │     │  Notebooks (Jupyter iframe)   │   │
                    │     │  Dashboards (Superset iframe) │   │
                    │     │  Pipelines  (Airflow  iframe) │   │
                    │     └──────────────────────────────┘   │
                    └──────────────────────────────────────┘
```

---

## Quick Start (Docker Compose)

```bash
git clone https://github.com/Lake-of-Tears/lake-of-tears.git
cd lake-of-tears
cp .env.example .env
# Edit .env — at minimum set MINIO_ROOT_PASSWORD, AUTH_SECRET_KEY,
# POSTGRES_AUTH_PASSWORD, and GEMINI_API_KEY
docker compose up -d
```

Open **http://localhost** — you'll be redirected to the login page. **Register the first account and you become the admin.** All subsequent registrations are viewers by default; promote them in the admin panel.

All services are accessible from the left sidebar. Direct ports are also available if needed:

| Service | Unified URL | Direct port |
|---------|-------------|-------------|
| Lake UI shell | http://localhost | http://localhost:3000 |
| Auth backend | http://localhost/api/ | http://localhost:8000 |
| JupyterLab | http://localhost/jupyter/ | http://localhost:8888 |
| Apache Superset | http://localhost/superset/ | http://localhost:8088 |
| Apache Airflow | http://localhost/airflow/ | http://localhost:8080 |
| MinIO Console | http://localhost:9001 | — |
| MinIO S3 API | — | http://localhost:9000 |

---

## Deploying with Helm (Kubernetes)

### Prerequisites

- Kubernetes cluster with an nginx Ingress controller (`ingress-nginx`)
- A StorageClass available for PersistentVolumeClaims (NFS, Longhorn, local-path, etc.)
- DNS records pointing your chosen hostnames at the cluster's ingress IP
- `helm` v3 and `kubectl` configured for your cluster

### How configuration works

The Helm chart separates **non-secret config** (hostnames, image tags, resource limits) from **secrets** (passwords, API keys, tokens). The recommended approach is two files:

| File | Contents | Committed to git? |
|------|----------|-------------------|
| `deploy/helm/values-prod.yaml` | Hostnames, storage class, resource overrides | Yes |
| `/etc/lake-of-tears/values-secret.yaml` | All passwords and API keys | **Never** |

### Step 1 — Create your secrets file

Create `/etc/lake-of-tears/values-secret.yaml` (or any path outside the repo) and populate every required value:

```yaml
# /etc/lake-of-tears/values-secret.yaml
# Keep this file off of version control.

minio:
  rootUser: "minio"
  rootPassword: "<strong password>"       # MinIO root credentials

jupyter:
  token: "<random token>"                 # JupyterLab access token

superset:
  secretKey: "<random secret>"            # Superset Flask secret key
  adminPassword: "<password>"             # Superset admin account password

airflow:
  secretKey: "<random secret>"            # Airflow Flask secret key
  adminPassword: "<password>"             # Airflow admin account password

postgres:
  auth:
    password: "<password>"                # Auth database (lake_auth) password
  airflow:
    password: "<password>"                # Airflow metadata database password

backend:
  auth:
    secretKey: "<random secret>"          # JWT signing key (shared between UI and backend)
  # SSO — configure only the providers you use (all optional)
  google:
    clientSecret: ""
  github:
    clientSecret: ""
  microsoft:
    clientSecret: ""
  oidc:
    clientSecret: ""

gemini:
  apiKey: "<your Google AI Studio API key>"

# Data source credentials (all optional)
stripe:
  secretKey: ""
shopify:
  storeDomain: ""
  accessToken: ""
hubspot:
  accessToken: ""
```

**Generating secrets** — use Python to create strong random values:

```bash
# Flask secret keys and JWT signing key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# JupyterLab token
python3 -c "import secrets; print(secrets.token_hex(32))"

# Database passwords
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

### Step 2 — Create your production values file

```yaml
# deploy/helm/values-prod.yaml
global:
  storageClass: "nfs-client"   # your StorageClass name

ingress:
  host: lake.example.com
  supersetHost: superset.example.com
  minioConsoleHost: minio.example.com
  minioApiHost: minio-api.example.com
  tls:
    - secretName: lake-of-tears-tls
      hosts:
        - lake.example.com
        - superset.example.com
        - minio.example.com
        - minio-api.example.com

backend:
  auth:
    baseUrl: "https://lake.example.com"   # used for OAuth callback URIs

lakeui:
  minioConsoleUrl: "https://minio.example.com"
```

### Step 3 — Install

```bash
helm install lake-of-tears ./deploy/helm/lake-of-tears \
  --namespace lake-of-tears \
  --create-namespace \
  --values deploy/helm/values-prod.yaml \
  --values /etc/lake-of-tears/values-secret.yaml \
  --wait --timeout 10m
```

To upgrade after a code or config change:

```bash
helm upgrade lake-of-tears ./deploy/helm/lake-of-tears \
  --namespace lake-of-tears \
  --values deploy/helm/values-prod.yaml \
  --values /etc/lake-of-tears/values-secret.yaml \
  --wait --timeout 10m
```

### Service URLs

All services are routed through the nginx Ingress controller:

| URL | Service |
|-----|---------|
| `https://lake.example.com/` | Lake UI shell |
| `https://lake.example.com/api/` | Auth backend |
| `https://lake.example.com/jupyter/` | JupyterLab |
| `https://superset.example.com/` | Apache Superset |
| `https://lake.example.com/airflow/` | Apache Airflow |
| `https://minio.example.com/` | MinIO Console |
| `https://minio-api.example.com/` | MinIO S3 API |

> **Note:** Superset runs on its own subdomain in the Helm deployment because its static assets are always served at `/static/…` with no prefix, which conflicts with the root Lake UI ingress. In Docker Compose, Superset is still accessible at `http://localhost/superset/` and directly at `http://localhost:8088`.

### Required values reference

Every value marked **required** must be present in your secrets file or the Helm render will fail with an error.

| Value | Required | Description |
|-------|----------|-------------|
| `minio.rootPassword` | yes | MinIO root password (8–40 chars) |
| `jupyter.token` | yes | JupyterLab access token |
| `superset.secretKey` | yes | Superset Flask secret key |
| `superset.adminPassword` | yes | Superset admin account password |
| `airflow.secretKey` | yes | Airflow Flask/Fernet secret key |
| `airflow.adminPassword` | yes | Airflow admin account password |
| `postgres.auth.password` | yes | Auth database (`lake_auth`) password |
| `postgres.airflow.password` | yes | Airflow metadata database password |
| `backend.auth.secretKey` | yes | JWT signing secret (generate with `token_hex(32)`) |
| `gemini.apiKey` | yes | Google AI Studio API key (for embeddings and RAG) |
| `ingress.host` | yes | Primary hostname for the Lake UI and embedded services |
| `ingress.supersetHost` | yes | Dedicated subdomain for Apache Superset (e.g. `superset.example.com`) |
| `ingress.minioConsoleHost` | yes | Hostname for the MinIO Console |

See `deploy/helm/lake-of-tears/values.yaml` for the full list of optional values (resource limits, image tags, SSO provider IDs, data source credentials, etc.).

---

## Configuration

All configuration is driven by environment variables. Copy `.env.example` to `.env` and fill in the required values before starting the stack.

**Core**

| Variable | Required | Default | Description |
|---|---|---|---|
| `MINIO_ROOT_USER` | yes | — | MinIO root username (5–20 chars) |
| `MINIO_ROOT_PASSWORD` | yes | — | MinIO root password (8–40 chars) |
| `MINIO_ENDPOINT` | no | `minio:9000` | MinIO S3 API endpoint (internal Docker hostname) |
| `GEMINI_API_KEY` | yes | — | Google AI Studio API key |
| `JUPYTER_TOKEN` | yes | — | JupyterLab access token |
| `SUPERSET_ADMIN_PASSWORD` | yes | — | Superset admin password |
| `SUPERSET_SECRET_KEY` | no | auto | Superset Flask secret key |
| `AIRFLOW_PASSWORD` | yes | — | Airflow admin password |
| `AIRFLOW_SECRET_KEY` | no | auto | Airflow Flask secret key |
| `MINIO_CONSOLE_URL` | no | `http://localhost:9001` | URL for the "Open MinIO Console" link in the Storage page |
| `DATALAKE_DATA_DIR` | yes | `~` | Host path for Docker volume mounts |

**Authentication**

| Variable | Required | Default | Description |
|---|---|---|---|
| `AUTH_SECRET_KEY` | yes | — | JWT signing secret shared between backend and UI. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `AUTH_BASE_URL` | no | `http://localhost` | Public URL of the stack — used to build OAuth callback URIs |
| `AUTH_ENABLED` | no | `true` | Set to `false` to disable login entirely (dev/trusted-network use) |
| `POSTGRES_AUTH_USER` | no | `lake_auth` | PostgreSQL username for the auth database |
| `POSTGRES_AUTH_PASSWORD` | yes | — | PostgreSQL password for the auth database |

**SSO Providers** (all optional — configure only what you use)

Register your application at each provider's developer console. The redirect URI for every provider is `{AUTH_BASE_URL}/api/auth/oauth/{provider}/callback`.

| Variable | Provider | Where to get it |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub | [github.com/settings/developers](https://github.com/settings/developers) |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` | Microsoft | [portal.azure.com — App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps) |
| `MICROSOFT_TENANT_ID` | Microsoft | Your Azure tenant ID, or `common` for any account |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Generic OIDC | Your identity provider (Okta, Keycloak, Auth0, etc.) |
| `OIDC_AUTH_URL` / `OIDC_TOKEN_URL` / `OIDC_USERINFO_URL` | Generic OIDC | From your provider's well-known configuration |
| `OIDC_DISPLAY_NAME` | Generic OIDC | Label shown on the "Continue with …" button (default: `SSO`) |

**Data source credentials** (all optional)

| Variable | Description |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe secret key for payments ingestion |
| `SHOPIFY_STORE_DOMAIN` | Shopify store domain (e.g. `mystore.myshopify.com`) |
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin API access token |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot private app access token |
| `POSTGRES_DSN` | PostgreSQL DSN for app database ingestion (unrelated to auth DB) |
| `WEATHER_LAT` / `WEATHER_LON` | Location for Open-Meteo weather ingestion |

---

## Authentication

Lake of Tears uses a dedicated FastAPI backend (`backend/`) for all authentication. JWT tokens are issued as httpOnly cookies and validated by the Lake UI on every request.

### First admin user

The first account registered on a fresh deployment becomes admin — automatically, with no configuration needed. Every subsequent signup (email/password or OAuth) is assigned the `viewer` role. Admins can promote or deactivate users via the user management API (`GET /api/users`, `PATCH /api/users/{id}`).

**Steps:**
1. Deploy the stack
2. Open the login page — you'll be redirected automatically if not logged in
3. Click **Create account**, enter your email and password, and submit
4. You are now the admin

This works identically for OAuth: if you sign in with Google (for example) before anyone else has registered, that Google account becomes the admin.

### Roles

| Role | Access |
|------|--------|
| `admin` | Full access; can change other users' roles and deactivate accounts |
| `viewer` | Read-only access to the Lake UI and all embedded services |

### SSO setup

SSO buttons appear on the login page automatically for any provider with a `CLIENT_ID` configured. Each provider requires registering a redirect URI:

```
{AUTH_BASE_URL}/api/auth/oauth/{provider}/callback
```

For example, with `AUTH_BASE_URL=https://lake.example.com` and Google:
```
https://lake.example.com/api/auth/oauth/google/callback
```

If a user signs in via OAuth with the same email as an existing email/password account, the accounts are automatically linked.

### Disabling auth

For development or trusted internal networks, set `AUTH_ENABLED=false` in `.env`. The login page is bypassed and all routes are open.

---

## Theme

The topbar includes a three-way theme toggle:

| Option | Behaviour |
|--------|-----------|
| ☀️ Light | Light content area, dark sidebar |
| 🌙 Dark | Full dark mode (default) |
| 🖥️ Auto | Follows OS `prefers-color-scheme`; falls back to time-of-day (dark 7 PM – 7 AM) if OS preference is not set |

Preference is saved to `localStorage` per browser.

---

## Data Sources

Lake of Tears is built to ingest operational business data from the APIs your company already uses. Every pipeline writes Parquet files partitioned by `year=/month=/day=` to `s3://datalake/raw/<source>/`, queryable immediately with DuckDB.

| Source | Script | Schedule | Output path | Requires |
|--------|--------|----------|-------------|----------|
| **Stripe** | `pipeline/ingest/stripe.py` | Hourly | `raw/stripe/` | `STRIPE_SECRET_KEY` |
| **Shopify** | `pipeline/ingest/shopify.py` | Every 6 h | `raw/shopify/` | `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ACCESS_TOKEN` |
| **HubSpot** | `pipeline/ingest/hubspot.py` | Daily 01:00 | `raw/hubspot/` | `HUBSPOT_ACCESS_TOKEN` |
| **PostgreSQL** | `pipeline/ingest/postgres.py` | Hourly | `raw/postgres/` | `POSTGRES_DSN` |
| **Open-Meteo** | `pipeline/ingest/weather.py` | Daily 06:00 | `raw/weather/` | none (free API) |

---

### Stripe — Payments & Revenue

The Stripe pipeline pulls charges, refunds, and subscription events from the Stripe API and lands them as Parquet for financial analysis and cohort reporting without touching your production database.

**What it collects (hourly):**

`raw/stripe_charges/` — one row per charge event:

| Column | Type | Example |
|--------|------|---------|
| `charge_id` | string | `"ch_3Pq..."` |
| `amount_usd` | float | `99.00` |
| `currency` | string | `"usd"` |
| `status` | string | `"succeeded"` |
| `customer_id` | string | `"cus_Qa..."` |
| `customer_email` | string | `"alice@example.com"` |
| `description` | string | `"Pro plan – monthly"` |
| `refunded` | bool | `false` |
| `dispute` | bool | `false` |
| `created_at` | timestamp | `2025-05-27 14:32:00` |

`raw/stripe_subscriptions/` — current subscription state snapshot:

| Column | Type | Example |
|--------|------|---------|
| `subscription_id` | string | `"sub_1P..."` |
| `customer_id` | string | `"cus_Qa..."` |
| `plan_name` | string | `"Pro"` |
| `interval` | string | `"month"` |
| `amount_usd` | float | `99.00` |
| `status` | string | `"active"` |
| `current_period_start` | timestamp | `2025-05-01 00:00:00` |
| `current_period_end` | timestamp | `2025-06-01 00:00:00` |
| `canceled_at` | timestamp | null |
| `created_at` | timestamp | `2024-11-15 09:12:00` |

**Example queries:**

```sql
-- MRR by plan this month
SELECT
    plan_name,
    count(*) AS active_subscribers,
    round(sum(amount_usd), 2) AS mrr_usd
FROM read_parquet('s3://datalake/raw/stripe_subscriptions/**/*.parquet')
WHERE status = 'active'
GROUP BY 1 ORDER BY mrr_usd DESC;
```

```sql
-- Daily revenue and refund rate over the last 30 days
SELECT
    date_trunc('day', created_at) AS day,
    round(sum(amount_usd) FILTER (WHERE status = 'succeeded'), 2) AS gross_revenue,
    round(sum(amount_usd) FILTER (WHERE refunded = true), 2) AS refunds,
    count(*) FILTER (WHERE dispute = true) AS disputes
FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet')
WHERE created_at >= current_date - INTERVAL 30 DAYS
GROUP BY 1 ORDER BY 1;
```

```sql
-- Monthly churn: subscriptions canceled vs. new this month
SELECT
    date_trunc('month', created_at) AS month,
    count(*) FILTER (WHERE status = 'active') AS new_subs,
    count(*) FILTER (WHERE canceled_at IS NOT NULL
        AND date_trunc('month', canceled_at) = date_trunc('month', current_date)) AS canceled
FROM read_parquet('s3://datalake/raw/stripe_subscriptions/**/*.parquet')
GROUP BY 1 ORDER BY 1 DESC LIMIT 6;
```

---

### Shopify — Orders & Inventory

The Shopify pipeline syncs order history, line items, and product inventory from the Shopify Admin API — enabling sales analysis, fulfillment tracking, and product performance reporting outside of Shopify's native reports.

**What it collects (every 6 hours):**

`raw/shopify_orders/` — one row per order:

| Column | Type | Example |
|--------|------|---------|
| `order_id` | string | `"5678901234"` |
| `order_number` | int32 | `1042` |
| `email` | string | `"bob@example.com"` |
| `financial_status` | string | `"paid"` |
| `fulfillment_status` | string | `"fulfilled"` |
| `subtotal_usd` | float | `149.95` |
| `total_discounts_usd` | float | `15.00` |
| `total_usd` | float | `134.95` |
| `line_item_count` | int32 | `3` |
| `source_name` | string | `"web"` |
| `created_at` | timestamp | `2025-05-27 11:04:00` |

`raw/shopify_line_items/` — one row per line item:

| Column | Type | Example |
|--------|------|---------|
| `order_id` | string | `"5678901234"` |
| `product_id` | string | `"7890123456"` |
| `variant_id` | string | `"9012345678"` |
| `title` | string | `"Wireless Keyboard"` |
| `sku` | string | `"KBD-WL-BLK"` |
| `quantity` | int32 | `1` |
| `price_usd` | float | `79.99` |
| `total_discount_usd` | float | `8.00` |
| `fulfillable_quantity` | int32 | `0` |

**Example queries:**

```sql
-- Top products by revenue this quarter
SELECT
    li.title,
    sum(li.quantity) AS units_sold,
    round(sum(li.price_usd * li.quantity - li.total_discount_usd), 2) AS net_revenue
FROM read_parquet('s3://datalake/raw/shopify_line_items/**/*.parquet') li
JOIN read_parquet('s3://datalake/raw/shopify_orders/**/*.parquet') o USING (order_id)
WHERE o.created_at >= date_trunc('quarter', current_date)
GROUP BY 1 ORDER BY net_revenue DESC LIMIT 20;
```

```sql
-- Average order value by traffic source
SELECT
    source_name,
    count(*) AS orders,
    round(avg(total_usd), 2) AS avg_order_value,
    round(sum(total_usd), 2) AS total_revenue
FROM read_parquet('s3://datalake/raw/shopify_orders/**/*.parquet')
WHERE financial_status = 'paid'
  AND created_at >= current_date - INTERVAL 90 DAYS
GROUP BY 1 ORDER BY total_revenue DESC;
```

```sql
-- Unfulfilled orders older than 48 hours
SELECT order_id, order_number, email, total_usd, created_at
FROM read_parquet('s3://datalake/raw/shopify_orders/**/*.parquet')
WHERE fulfillment_status IS NULL
  AND financial_status = 'paid'
  AND created_at < now() - INTERVAL 48 HOURS
ORDER BY created_at;
```

---

### HubSpot — CRM & Pipeline

The HubSpot pipeline syncs contacts, companies, deals, and activity logs daily — giving you a historical record of your sales pipeline that you can join against revenue data from Stripe or web analytics.

**What it collects (daily):**

`raw/hubspot_deals/` — one row per deal snapshot:

| Column | Type | Example |
|--------|------|---------|
| `deal_id` | string | `"12345678"` |
| `deal_name` | string | `"Acme Corp – Enterprise"` |
| `stage` | string | `"contractsent"` |
| `amount_usd` | float | `24000.00` |
| `close_date` | date | `2025-06-30` |
| `owner_name` | string | `"Sarah M."` |
| `pipeline` | string | `"default"` |
| `created_at` | timestamp | `2025-04-12 09:30:00` |
| `last_modified_at` | timestamp | `2025-05-26 16:45:00` |

`raw/hubspot_contacts/` — one row per contact:

| Column | Type | Example |
|--------|------|---------|
| `contact_id` | string | `"98765432"` |
| `email` | string | `"carol@acme.com"` |
| `first_name` | string | `"Carol"` |
| `last_name` | string | `"Jones"` |
| `company` | string | `"Acme Corp"` |
| `lifecycle_stage` | string | `"opportunity"` |
| `lead_source` | string | `"organic search"` |
| `created_at` | timestamp | `2025-03-01 10:00:00` |

**Example queries:**

```sql
-- Open pipeline by stage
SELECT
    stage,
    count(*) AS deals,
    round(sum(amount_usd), 2) AS total_value,
    round(avg(amount_usd), 2) AS avg_deal_size
FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet')
WHERE stage NOT IN ('closedwon', 'closedlost')
GROUP BY 1 ORDER BY total_value DESC;
```

```sql
-- Win rate and average sales cycle by owner
SELECT
    owner_name,
    count(*) FILTER (WHERE stage = 'closedwon') AS won,
    count(*) FILTER (WHERE stage = 'closedlost') AS lost,
    round(count(*) FILTER (WHERE stage = 'closedwon') * 100.0 / count(*), 1) AS win_rate_pct,
    round(avg(last_modified_at - created_at) FILTER (WHERE stage = 'closedwon'), 0) AS avg_cycle_days
FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet')
WHERE stage IN ('closedwon', 'closedlost')
GROUP BY 1 ORDER BY won DESC;
```

```sql
-- New contacts by lead source this month
SELECT lead_source, count(*) AS new_contacts
FROM read_parquet('s3://datalake/raw/hubspot_contacts/**/*.parquet')
WHERE created_at >= date_trunc('month', current_date)
GROUP BY 1 ORDER BY new_contacts DESC;
```

---

### PostgreSQL — Application Database

The PostgreSQL pipeline runs configurable incremental queries against your application database and snapshots the results to the lake — useful for capturing user event tables, audit logs, or any operational data you want to analyze without putting load on production.

**What it collects (hourly):** Schema is fully configurable. A typical deployment captures user signups, feature events, and error logs.

**Example queries:**

```sql
-- Join app signups (from Postgres) with closed-won deals (from HubSpot)
SELECT
    date_trunc('week', u.created_at) AS week,
    count(distinct u.user_id) AS signups,
    count(distinct d.deal_id) AS closed_won
FROM read_parquet('s3://datalake/raw/postgres/users/**/*.parquet') u
LEFT JOIN read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet') d
  ON u.email = d.owner_name AND d.stage = 'closedwon'
GROUP BY 1 ORDER BY 1 DESC LIMIT 12;
```

---

### Open-Meteo — Weather (Join Dimension)

Downloads hourly forecasts for a given location from [Open-Meteo](https://open-meteo.com) — no API key required. Columns include temperature, precipitation, wind speed, UV index, and cloud cover. Useful as a join dimension for any business with location-sensitive demand (retail foot traffic, delivery logistics, outdoor services, etc.).

Set your location in `.env`:
```
WEATHER_LAT=40.7128
WEATHER_LON=-74.0060
```

```sql
-- Do Shopify orders drop on rainy days?
SELECT
    round(w.precipitation_sum, 0) AS rain_mm,
    count(o.order_id) AS orders,
    round(avg(o.total_usd), 2) AS avg_order_value
FROM read_parquet('s3://datalake/raw/shopify_orders/**/*.parquet') o
JOIN read_parquet('s3://datalake/raw/weather/**/*.parquet') w
  ON date_trunc('day', o.created_at) = w.date
GROUP BY 1 ORDER BY 1;
```

---

## Pipeline

After raw data lands in `s3://datalake/raw/`, three downstream pipelines run in sequence each morning:

### 1. Embed (`pipeline/embed/embed_sources.py`) — 06:30 daily

Reads yesterday's Parquet files, builds a text representation of each row, and calls `gemini-embedding-001` to produce a 768-dimensional embedding vector. Results are written to `s3://datalake/embeddings/<source>/` as Parquet. The DuckDB VSS extension (HNSW index) is used for all vector similarity queries; both sides of every `array_cosine_similarity` call must cast to `DOUBLE[768]`.

### 2. Summarize (`pipeline/embed/summarize.py`) — 07:00 daily

Reads yesterday's raw data and generates a concise natural-language summary per source using `gemini-2.5-flash`. Summaries are stored back to MinIO and surfaced in the Lake UI dashboard.

### 3. Anomaly Detection (`pipeline/embed/anomaly_detect.py`) — 08:00 daily

Runs scikit-learn Isolation Forest over numerical columns to flag statistical outliers. Flagged rows are passed to `gemini-2.5-flash` for a plain-English explanation of what the anomaly might mean. Results appear in the Anomaly Detection page of the Lake UI.

### RAG Query (`pipeline/query/rag_query.py`) — on-demand

Given a natural-language question, embeds the query with `gemini-embedding-001`, performs cosine similarity search over the embeddings in DuckDB, retrieves the top-K most relevant rows, and passes them as context to `gemini-2.5-flash` to generate a grounded answer. Accessible from the **AI Query** page in the Lake UI.

---

## Adding a New Data Source

1. Create `pipeline/ingest/my_source.py`. Follow the pattern of an existing ingest script — read from your source, build a list of dicts, write Parquet to `s3://datalake/raw/my_source/year=YYYY/month=MM/day=DD/data.parquet` using the `StorageWriter` helper from `pipeline/shared/storage_writer.py`.

2. Create a DAG in `pipeline/dags/ingest_my_source_dag.py`. Copy an existing DAG and update the schedule interval, script reference, and task IDs.

3. Add your source name to the `SOURCES` list in `pipeline/embed/embed_sources.py` so embeddings are generated for the new data.

4. If your source has a text field worth embedding, define a `row_to_text()` function for it following the existing `stripe_row_to_text` / `hubspot_row_to_text` pattern.

---

## Architecture Notes

- **Single entry point.** nginx sits in front of everything at port 80. JupyterLab (`/jupyter/`) and Airflow (`/airflow/`) are proxied at path prefixes. Superset runs on its own subdomain (`superset.*`) in the Helm deployment — its static assets are always served at `/static/…` with no prefix, which conflicts with the root Lake UI ingress. In Docker Compose, Superset is still proxied at `/superset/` via `APPLICATION_PREFIX_PATH`. MinIO Console is exposed separately at port 9001 (Docker Compose) or its own Ingress host (Kubernetes) due to SPA routing limitations.

- **Visualizations page (Superset) requires no login.** `PUBLIC_ROLE_LIKE = "Gamma"` is set in `deploy/superset/superset_config.py`, granting anonymous users full read access to dashboards and charts. This is appropriate for a trusted internal network. Remove that setting if you need login enforcement.

- **All data is Parquet.** Files are partitioned by `year=/month=/day=` and written with `pyarrow`. DuckDB's Hive partitioning support makes date-range queries highly efficient.

- **No separate vector database.** Embeddings are stored as `DOUBLE[]` columns in Parquet files. The DuckDB VSS extension builds an in-process HNSW index at query time. There is no Pinecone, Weaviate, or pgvector to operate.

- **Embedding type discipline.** Parquet stores vectors as `DOUBLE[]` (unbounded list). DuckDB VSS requires fixed-size arrays. Every cosine similarity query must cast both sides: `array_cosine_similarity(embedding::DOUBLE[768], $query_vec::DOUBLE[768])`.

- **MinIO CE only.** The bucket layout uses a single `datalake` bucket with logical prefixes (`raw/`, `embeddings/`). Do not create additional top-level buckets — DuckDB S3 glob patterns depend on this layout.

- **Single-node first.** The Docker Compose deployment is the primary supported path. The Helm chart targets a standard Kubernetes cluster with an NFS-backed StorageClass for the MinIO PVC, but the data model is identical.

---

## Contributing

We welcome contributions of all kinds — new data source connectors, UI improvements, pipeline enhancements, and documentation.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, and review our [Code of Conduct](CODE_OF_CONDUCT.md).

- **Community:** [thelakeoftears.com](https://thelakeoftears.com) · Discord (link on the website)
- **Issues:** [github.com/Lake-of-Tears/lake-of-tears/issues](https://github.com/Lake-of-Tears/lake-of-tears/issues)
- **Security:** See [SECURITY.md](SECURITY.md) for responsible disclosure.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
