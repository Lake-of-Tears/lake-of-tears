# Feature Specification: Databricks-Style AI Query & SQL AI Functions (#111)

## 1. Executive Summary & Goal

Upgrade Lake of Tears from a single-turn RAG snippet search into a comprehensive, Databricks-modeled AI Query & SQL AI ecosystem across three pillars:

1. **Inline DuckDB SQL AI Functions (`ai_query`, `ai_summarize`, `ai_classify`, `ai_extract`)**: Call Google Gemini foundation models directly inside SQL expressions across dataset rows using vectorized PyArrow UDFs, thread-pool concurrency, in-memory LRU caching, and per-query row guardrails.
2. **AI-Assisted SQL Editor (`/sql`)**: Schema-aware Text-to-SQL generation, one-click **"Fix with AI"** when DuckDB returns syntax/binder errors, **"Explain Query"** modal, and a collapsible Catalog Schema & AI Function Reference sidebar.
3. **Conversational Lake Agent (`/ai` — Databricks Genie / AI-BI Model)**: Multi-turn conversational data analyst that discovers workspace Catalog schemas, synthesizes & auto-corrects read-only DuckDB SQL, executes live queries over S3 Parquet / Catalog views, renders Apache ECharts visualizations, and supports hybrid/RAG semantic embedding search.
4. **CLI-Ready Headless REST API (`/api/v1/ai/*`)**: Exposes all SQL execution, AI SQL assistant, and Genie conversational endpoints on `backend` (`backend/ai_router.py`) with `Authorization: Bearer <token>` and `X-Workspace-Slug` / `X-Workspace-Id` header support for the Lake of Tears CLI.
5. **Centralized Superadmin `GEMINI_API_KEY` Management (`/settings/admin`)**: Superadmins can configure, test, replace, and clear the instance-wide `GEMINI_API_KEY` directly in the Admin Panel, encrypted at rest in PostgreSQL (`system_settings`), replacing all environment-variable configuration across services and pipelines.

---

## 2. Architecture & Design Decisions (Resolved via /grill-me)

| Decision Area | Selected Approach | Key Rationale |
| :--- | :--- | :--- |
| **Catalog Table Resolution** | **Auto-register Workspace Catalog Views + S3 Parquet Fallback** | Registers active workspace `CatalogTable` entries as DuckDB views (`schema.table` -> `read_parquet(s3_path_pattern)`) and injects both Catalog metadata and default S3 datasets (`s3://datalake/raw/...`) into Gemini prompt context. |
| **DuckDB AI UDF Signatures & Batching** | **Smart 1-or-2 Arg `ai_query` + PyArrow Vectorized Batching + LRU Cache** | Supports both `ai_query(prompt)` and Databricks-style `ai_query(model, prompt)` / `ai_query(prompt, model)`. Deduplicates prompts per vector chunk, caches responses in a 1,024-entry LRU cache, and dispatches uncached calls via `ThreadPoolExecutor(max_workers=5)`. |
| **Runaway Query Guardrails** | **Configurable Per-Query Row Cap (`AI_MAX_ROWS_PER_QUERY=50`)** | Prevents accidental thousand-row LLM queries from exhausting Gemini API quotas; raises a clear SQL error prompting the user to add a `LIMIT` clause. |
| **SQL Editor Assistant UX (`/sql`)** | **Inline AI Prompt Bar + Fix with AI + Explain Modal + Reference Sidebar** | Provides async JSON actions without page reload while preserving standard `POST /sql` form execution. |
| **Genie Conversational Agent (`/ai`)** | **Multi-Turn Text-to-SQL with 1-Step Self-Correction + ECharts + RAG Toggle** | Defaults to Genie SQL mode (with automatic 1-retry SQL self-correction on DuckDB errors and Apache ECharts auto-plotting), plus `Hybrid` and `RAG Embeddings` modes. |
| **CLI-Ready API Layer** | **`/api/v1/ai/*` Router in `backend/` + `ui/` Integration** | Matches `storage_router.py`, `pipelines_router.py`, and `dashboards_router.py` so a future Lake of Tears CLI can invoke `/api/v1/ai/sql/*` and `/api/v1/ai/chat` using Bearer tokens. |
| **API Key Storage & Precedence** | **Admin Panel Only (Encrypted in DB) — Remove `GEMINI_API_KEY` Env Var** | `GEMINI_API_KEY` is removed from `.env.example`, `docker-compose.yml`, Helm charts/values, and Terraform. All consumers (`backend`, `ui`, `pipeline`) resolve the key exclusively from the Admin Panel setting. |
| **Secret Security & Validation** | **Encrypted at Rest (`AUTH_SECRET_KEY`) + Masked Display + Live Test Button** | Encrypted in `system_settings.value_str`; never returned in plaintext to browsers (`gemini_api_key_configured` + `••••1234`). Saves immediately without blocking on network I/O and provides an explicit **"Test Key"** button in `/settings/admin`. |
| **Default Gemini Model** | **Admin Panel Setting `gemini_default_model` (plain text in `system_settings.value_str`) — No Model Env Var / Hardcoding** | Single instance-wide default (built-in fallback `gemini-3.8-flash`) used by SQL AI Functions, the SQL Assistant, Genie, and pipelines. An explicit per-call model (e.g. `ai_query('model', ...)` or a request `model`) overrides it. The embedding model (`gemini-embedding-001`) stays fixed because stored vectors are 768-dim and model-specific. `AI_DEFAULT_MODEL` env var is removed. |

---

## 3. DuckDB SQL AI Functions Reference & Analytical Workflows

All DuckDB connections created by Lake of Tears (`backend/ai_service.py` and `ui/main.py`) automatically register four scalar/vectorized AI functions:

### 3.1 `ai_query([model,] prompt) -> VARCHAR`
Invokes a Gemini model per row with a custom prompt. Supports 1 argument (`prompt`, using default `gemini-3.8-flash`) or 2 arguments (`model, prompt` in Databricks order or `prompt, model`). Returns `NULL` if any argument is `NULL`.

```sql
-- Databricks 2-argument signature (model, prompt)
SELECT
    id,
    amount_usd,
    description,
    ai_query('gemini-3.8-flash', 'Classify payment risk in one word: ' || description) AS risk_label
FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet')
LIMIT 10;

-- 1-argument shorthand signature
SELECT
    deal_name,
    ai_query('Write a 5-word follow-up action for deal: ' || deal_name) AS next_step
FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet')
LIMIT 10;
```

### 3.2 `ai_summarize(text) -> VARCHAR`
Generates a concise 1–2 sentence summary of unstructured text per row.

```sql
SELECT
    source,
    ingested_at,
    ai_summarize(summary) AS executive_brief
FROM read_parquet('s3://datalake/embeddings/**/*.parquet')
ORDER BY ingested_at DESC
LIMIT 5;
```

### 3.3 `ai_classify(text, categories[]) -> VARCHAR`
Zero-shot categorical classification that classifies `text` into strictly one of the provided `VARCHAR[]` category labels and normalizes the returned label.

```sql
SELECT
    deal_name,
    stage,
    amount_usd,
    ai_classify(stage, ['early_stage', 'negotiation', 'closed']) AS funnel_bucket
FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet')
LIMIT 15;
```

### 3.4 `ai_extract(text, fields[]) -> VARCHAR (JSON)`
Extracts structured fields from unstructured text and returns a normalized, valid JSON object string containing every requested field key (can be queried directly with DuckDB JSON operators `->>` or `json_extract_string`).

```sql
WITH extracted AS (
    SELECT
        source,
        ai_extract(summary, ['metric', 'value', 'status']) AS payload
    FROM read_parquet('s3://datalake/embeddings/**/*.parquet')
    LIMIT 5
)
SELECT
    source,
    payload,
    json_extract_string(payload, '$.metric') AS metric_name,
    json_extract_string(payload, '$.status') AS status
FROM extracted;
```

---

## 4. Headless & CLI-Ready REST API (`/api/v1/ai/*`)

All endpoints accept `Authorization: Bearer <jwt>` (or `lake_token` session cookie) and workspace headers (`X-Workspace-Slug`, `X-Workspace-Id`, or `?workspace_slug=`):

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/ai/context` | Returns workspace Catalog schemas/tables, default S3 datasets, and registered SQL AI function definitions. |
| `POST` | `/api/v1/ai/sql/execute` | Executes DuckDB SQL with workspace Catalog views and `ai_*` UDFs (`{sql, read_only, max_ai_rows}`). |
| `POST` | `/api/v1/ai/sql/generate` | Generates DuckDB SQL from natural language (`{prompt, current_sql, model}`). |
| `POST` | `/api/v1/ai/sql/fix` | Diagnoses and fixes a failing DuckDB SQL query (`{sql, error, model}`). |
| `POST` | `/api/v1/ai/sql/explain` | Explains a DuckDB SQL query step-by-step (`{sql, model}`). |
| `POST` | `/api/v1/ai/chat` | Runs the multi-turn Genie conversational data analyst loop (`{question, mode, history, model}`). |
| `POST` | `/api/v1/ai/chat/cancel` | Aborts an in-flight Genie request and interrupts underlying DuckDB execution (`{request_id}`). |

---

## 5. Superadmin `GEMINI_API_KEY` Management (`/settings/admin`)

### 5.1 Storage & Encryption at Rest
- `system_settings` table adds `value_str TEXT NULL` (migration `0007_system_settings_value_str.py`).
- Setting key `gemini_api_key` stores an authenticated ciphertext (`v1:<nonce_b64>:<ciphertext_b64>:<hmac_b64>`) encrypted and MAC-verified with keys derived via SHA-256/HMAC from `AUTH_SECRET_KEY`.
- Plaintext keys are never returned to the browser. `GET /api/admin/settings` returns:
  - `gemini_api_key_configured: bool`
  - `gemini_api_key_masked: str | None` (e.g., `••••ABCD`)
  - `gemini_default_model: str` (stored value, or the built-in default if unset)

### 5.2 Admin Panel Endpoints & UI
- `PATCH /api/admin/settings` accepts `gemini_api_key: str | None` (to set/replace) and `clear_gemini_api_key: bool = False` (to clear).
- `PATCH /api/admin/settings` also accepts `gemini_default_model: str | None`; an empty string resets to the built-in default. Saving clears the in-memory AI cache and default-model caches (backend and UI use a 30 s TTL cache so per-row UDF calls do not hit the DB).
- `POST /api/admin/settings/test-gemini-key` tests either a candidate `api_key` passed in the body or the currently stored `gemini_api_key` against the Gemini API and returns `{"ok": bool, "model": str, "message": str}`.
- `GET /api/internal/settings/gemini-key` allows internal services (`ui`, `pipeline`) authenticated with `X-Internal-Secret: <AUTH_SECRET_KEY>` or a valid user JWT to retrieve the active key and `gemini_default_model` at runtime.


---

## 6. Genie Conversational Agent Cancellation & Stop Semantics

### 6.1 Frontend UX (`/ai`)
- While a query is running, the **Ask Genie** button switches state to a red **Stop** button.
- Clicking **Stop** aborts the browser request immediately via `AbortController.abort()` and posts an asynchronous cancellation notification to `/api/v1/ai/chat/cancel`.
- The UI appends a distinct, muted `"Request cancelled."` bubble in the chat transcript. Cancelled turns are excluded from subsequent multi-turn history payloads so failed/aborted queries do not distort conversational context.

### 6.2 Backend & DuckDB Execution Engine
- Genie's synchronous execution pipeline runs off the main event loop in a background thread via `run_in_threadpool`, keeping FastAPI fully responsive.
- A client-disconnect watcher monitors socket status; navigating away, closing the tab, or aborting the request triggers immediate cooperative cancellation.
- If DuckDB is actively executing a query during cancellation, `con.interrupt()` is called immediately to stop engine execution without hanging the server thread.
- Checkpoints between schema discovery, SQL generation, and narrative synthesis halt execution immediately, preventing unnecessary downstream Gemini API calls.
