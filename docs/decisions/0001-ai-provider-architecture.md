# 0001: Pluggable AI provider architecture

- **Status:** Accepted by the ticket owner (oTraXo) for implementation; open for team review. Nothing in this record is irreversible: each decision lists what reverting it would involve.
- **Tickets:** #71 (multi-provider LLM support, Admin Panel configuration), #68 (provider architecture), #69 (Ollama and OpenAI-compatible), #70 (Azure AI Foundry and OpenAI).
- **Date:** 2026-10-17

## Context

Lake of Tears talks to Google Gemini directly from four places: `backend/ai_service.py`, `ui/main.py`, `ui/duckdb_ai.py` and `pipeline/shared/gemini_embedder.py` (plus the Airflow scripts that use its client). That is about 9 `generate_content` and 4 `embed_content` call sites spread over three separate images (backend, UI, Airflow). Vector search hard-codes 768 dimensions (`DOUBLE[768]`) in five places.

The tickets ask for several providers (Gemini, Ollama and OpenAI-compatible servers, OpenAI, Azure AI Foundry, Amazon Bedrock). #71 requires every provider setting to be editable in the Admin Panel, stored encrypted in PostgreSQL, applied without a restart. #68 proposes a provider interface selected by `LLM_PROVIDER` in `.env` and Helm values, with the vector dimension following the provider.

Three decisions had to be made before writing code. They are recorded below with the options that were **not** chosen.

## Decision 1: where the provider code and credentials live

**Chosen: the backend is the AI gateway.** Provider implementations and credentials exist only in the backend. The UI and the pipelines call internal backend endpoints (`/api/internal/ai/...`, authenticated by the shared internal secret) for embeddings and text generation.

| | Chosen: backend gateway | Alternative: shared library in every image |
|---|---|---|
| Idea | One implementation in `backend/`; the UI and Airflow call it over HTTP | A common `lake_ai` package built into the backend, UI and Airflow images, each calling the vendor directly |
| For | One implementation to test and keep correct; credentials stay in one container; adding a provider touches one place; no per-image vendor SDKs | No extra HTTP hop; callers work if the backend is briefly unavailable |
| Against | One extra network call per AI request; the backend must be up for AI features | Credentials must reach three containers; three images must be rebuilt and kept in sync; each needs the vendor SDKs |
| To revert | Move `backend/ai_providers/` into a package built into the UI and pipeline images, and replace the gateway calls with direct provider calls. The provider interfaces do not change. | n/a |

## Decision 2: source of truth for provider settings

**Chosen: Admin Panel first, environment as fallback.** Settings saved in the Admin Panel (encrypted in `system_settings`, applied at runtime) win. Environment variables (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_EMBEDDING_MODEL`, `LLM_EMBEDDING_DIMENSION`, `GEMINI_API_KEY`) only supply defaults for a fresh or air-gapped install.

| | Chosen: Admin Panel first, env fallback | Alternative: Admin Panel only |
|---|---|---|
| Idea | Both sources exist; the Admin Panel overrides the environment | No provider settings in `.env` or Helm at all |
| For | Satisfies #71 (no redeploy to change provider) and keeps #68's `LLM_PROVIDER`; allows unattended bootstrap | The strictest reading of #71: a single place to configure, no ambiguity about which value is live |
| Against | Two sources can disagree (the Admin Panel value hides the environment one) | A brand-new install has no AI until an admin configures it; harder to script |
| To revert | Delete the environment fallback block in `load_provider_config` (`backend/ai_providers/registry.py`). | n/a |

An alternative that was **not** offered but is implied by #68 alone is *environment only* (`LLM_PROVIDER` in `.env` and Helm, restart to change). It contradicts #71's no-restart requirement, so it was not chosen.

## Decision 3: embedding dimensions

**Chosen: tag each embedding row and filter at query time.** Embedding files get `provider`, `model` and `dimension` columns. Search filters to the active model's dimension and casts to `DOUBLE[<dimension>]`, so existing 768-dimension vectors keep working next to new ones, and switching provider does not break queries.

| | Chosen: tag rows, filter at query time | Alternative: one dimension per lake |
|---|---|---|
| Idea | Vectors of different models/dimensions coexist in `embeddings/` | The dimension is a single global setting; changing provider means re-embedding everything |
| For | No flag day when switching provider; old data stays searchable; mixed lakes are valid | Simplest queries and schema; no per-row metadata |
| Against | More columns and a `WHERE` on every search; results only come from rows made by the active model until the rest is re-embedded | A provider switch makes all old embeddings unusable until a full re-embed finishes |
| To revert | Stop writing the extra columns, drop the filter, and treat the dimension as a global setting. A one-off re-embed makes the data uniform. | n/a |

## Implementation notes (kept up to date as slices land)

- **Slice 1 (#179):** `backend/ai_providers` (interfaces, Gemini provider, registry, encrypted settings) and the internal gateway (`/api/internal/ai/info`, `/embed`, `/generate`, internal secret only).
- **Slice 2:** the backend, UI and pipelines no longer import a vendor SDK; the UI and pipelines call the gateway (`ui/ai_gateway_client.py`, `pipeline/shared/ai_gateway.py`) and the backend uses its provider objects. `google-genai` is no longer installed in the UI image. Embedding files gain `provider`, `model` and `dimension` columns. Search compares only vectors of the active model's dimension and, when the files record it, the active model; files written before this change have no `model` column and count as `gemini-embedding-001`. The search SQL exists in three copies (backend, UI, pipeline) because the images are separate; tests keep them identical.
- **Slice 3:** the Admin Panel (Settings, Admin) has an **AI Provider** section driven by each provider's field schema (`FieldSpec` in `backend/ai_providers/registry.py`), so later provider slices only add a schema and an implementation, not UI code. Backend: `GET/PUT /api/admin/ai-provider` and `POST /api/admin/ai-provider/test` (superadmin only; secrets encrypted, masked on read, a blank secret keeps the stored one, `null` clears it). "Test Connection" tries the entered, unsaved settings and reports chat and embedding results with latency and the returned dimension. Changes apply immediately. The older Gemini-only Admin endpoints and the old UI routes were replaced; the settings the old form wrote are still read as a fallback.
- **To undo decision 3 later:** delete the `WHERE len(embedding) = N ...` filter and the `provider`/`model`/`dimension` columns in `embed_sources.py`, and fix the dimension as a setting. Existing files remain readable either way.

## Delivery plan (slices)

1. **#68:** provider interfaces, `GeminiProvider`, provider registry and settings storage, internal gateway endpoints. No behaviour change.
2. **#68:** move every call site to the gateway; dynamic dimension, embedding metadata columns, filtered search.
3. **#71:** Admin Panel provider selector, per-provider fields, encrypted secrets, "Test Connection".
4. **#69:** Ollama and OpenAI-compatible endpoints.
5. **#70:** OpenAI and Azure AI Foundry.
6. **#71:** Amazon Bedrock (IAM role and static credential modes).
7. Wiki documentation of provider setup.

## Notes and open questions for the team

- Real cloud providers (Azure, OpenAI, Bedrock) cannot be exercised without accounts; those slices will be covered by unit tests with stubs and need a reviewer with credentials to verify.
- A new internal gateway should accept only the internal secret and never return credentials. This is enforced by tests in `tests/test_ai_providers.py`.
