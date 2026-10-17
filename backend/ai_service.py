from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import duckdb
import pyarrow as pa
from ai_providers import AIProvider, ProviderNotConfigured, get_active_provider
from ai_providers.compat import ProviderClient
from ai_providers.gemini import GeminiProvider
from auth import DEFAULT_GEMINI_MODEL
from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.ai.service")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
GEMINI_KEY_NOT_CONFIGURED_MSG = (
    "No AI provider is configured. A superadmin can set one up in Settings → Admin Panel."
)

_MODEL_CACHE_TTL = 30.0
_MODEL_CACHE: tuple[str, float] | None = None


def get_default_model() -> str:
    """Default Gemini model from the Admin Panel setting (cached briefly), else built-in default."""
    global _MODEL_CACHE
    now = time.monotonic()
    cached = _MODEL_CACHE
    if cached is not None and now - cached[1] < _MODEL_CACHE_TTL:
        return cached[0]
    try:
        model = get_active_provider().chat_model or DEFAULT_GEMINI_MODEL
    except Exception:
        model = DEFAULT_GEMINI_MODEL
    _MODEL_CACHE = (model, now)
    return model


def clear_model_cache() -> None:
    global _MODEL_CACHE
    _MODEL_CACHE = None


AI_MAX_ROWS_PER_QUERY = int(os.getenv("AI_MAX_ROWS_PER_QUERY", "50"))
AI_MAX_CONCURRENCY = int(os.getenv("AI_MAX_CONCURRENCY", "5"))
AI_CACHE_MAX_SIZE = 1024


_PROVIDER_ACCESS = "provider"


def _resolve_api_key(api_key: str | None = None) -> str:
    """Non-empty when AI can be used: an explicit Gemini key override, or a configured provider.

    A configured provider is represented by a placeholder (never a credential); the credentials
    stay inside the provider object returned by ``_provider_for``.
    """
    if api_key is not None and api_key.strip():
        return api_key.strip()
    try:
        get_active_provider()
    except Exception:
        return ""
    return _PROVIDER_ACCESS


def _provider_for(api_key: str | None = None) -> AIProvider:
    """The provider to use: a Gemini one for an explicit key override, else the active provider."""
    if api_key and api_key.strip() and api_key != _PROVIDER_ACCESS:
        return GeminiProvider(api_key.strip(), chat_model=get_default_model())
    return get_active_provider()


def _client_for(api_key: str | None = None) -> ProviderClient:
    return ProviderClient(_provider_for(api_key))


def test_gemini_api_key(
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Perform a lightweight live test of a Gemini API key."""
    model = model or get_default_model()
    key = _resolve_api_key(api_key)
    if not key:
        return {
            "ok": False,
            "model": model,
            "message": GEMINI_KEY_NOT_CONFIGURED_MSG,
        }
    try:
        provider = (
            GeminiProvider(key, chat_model=model)
            if key != _PROVIDER_ACCESS
            else get_active_provider()
        )
        reply = provider.generate("Reply with the single word OK.", model=model).strip()
        return {
            "ok": True,
            "model": model,
            "message": f"Connected to {model} successfully ({reply or 'OK'}).",
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": model,
            "message": f"Gemini API validation failed: {exc}",
        }


# Thread-safe in-memory LRU cache for SQL AI scalar calls
_AI_CACHE_LOCK = threading.Lock()
_AI_CACHE: OrderedDict[tuple[str, str, str], str] = OrderedDict()

# Known default lakehouse S3 datasets & column metadata for fallback context
DEFAULT_LAKEHOUSE_DATASETS: list[dict[str, Any]] = [
    {
        "name": "stripe_charges",
        "schema": "bronze",
        "s3_path": f"s3://{MINIO_BUCKET}/raw/stripe_charges/**/*.parquet",
        "description": "Stripe payment charge records including amount, currency, status, and customer details.",
        "columns": [
            {"name": "id", "type": "VARCHAR"},
            {"name": "amount_usd", "type": "DOUBLE"},
            {"name": "currency", "type": "VARCHAR"},
            {"name": "status", "type": "VARCHAR"},
            {"name": "customer_email", "type": "VARCHAR"},
            {"name": "description", "type": "VARCHAR"},
            {"name": "refunded", "type": "BOOLEAN"},
            {"name": "created_at", "type": "TIMESTAMP"},
        ],
    },
    {
        "name": "shopify_line_items",
        "schema": "bronze",
        "s3_path": f"s3://{MINIO_BUCKET}/raw/shopify_line_items/**/*.parquet",
        "description": "Shopify e-commerce order line items with product title, quantity, and unit price.",
        "columns": [
            {"name": "order_id", "type": "VARCHAR"},
            {"name": "title", "type": "VARCHAR"},
            {"name": "sku", "type": "VARCHAR"},
            {"name": "quantity", "type": "BIGINT"},
            {"name": "price_usd", "type": "DOUBLE"},
            {"name": "created_at", "type": "TIMESTAMP"},
        ],
    },
    {
        "name": "hubspot_deals",
        "schema": "bronze",
        "s3_path": f"s3://{MINIO_BUCKET}/raw/hubspot_deals/**/*.parquet",
        "description": "HubSpot CRM deals pipeline with deal name, stage, owner, and USD value.",
        "columns": [
            {"name": "deal_id", "type": "VARCHAR"},
            {"name": "deal_name", "type": "VARCHAR"},
            {"name": "stage", "type": "VARCHAR"},
            {"name": "amount_usd", "type": "DOUBLE"},
            {"name": "owner_name", "type": "VARCHAR"},
            {"name": "closed_at", "type": "TIMESTAMP"},
            {"name": "created_at", "type": "TIMESTAMP"},
        ],
    },
    {
        "name": "weather",
        "schema": "bronze",
        "s3_path": f"s3://{MINIO_BUCKET}/raw/weather/**/*.parquet",
        "description": "Hourly weather observations and forecasts.",
        "columns": [
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "temperature_c", "type": "DOUBLE"},
            {"name": "humidity", "type": "DOUBLE"},
            {"name": "condition", "type": "VARCHAR"},
        ],
    },
    {
        "name": "embeddings",
        "schema": "gold",
        "s3_path": f"s3://{MINIO_BUCKET}/embeddings/**/*.parquet",
        "description": "Embeddings of each source's record text (content), written daily by the embed_sources pipeline; provider, model and dimension record which model made them.",
        "columns": [
            {"name": "id", "type": "VARCHAR"},
            {"name": "source", "type": "VARCHAR"},
            {"name": "content", "type": "VARCHAR"},
            {"name": "embedding", "type": "DOUBLE[]"},
            {"name": "source_date", "type": "VARCHAR"},
            {"name": "provider", "type": "VARCHAR"},
            {"name": "model", "type": "VARCHAR"},
            {"name": "dimension", "type": "BIGINT"},
            {"name": "ingested_at", "type": "TIMESTAMP"},
        ],
    },
]

SQL_AI_FUNCTIONS: list[dict[str, str]] = [
    {
        "name": "ai_query",
        "signature": "ai_query([model,] prompt)",
        "description": "Invoke a Gemini foundation model per row with a custom prompt (supports 1-arg prompt or 2-arg Databricks-style model + prompt).",
        "example": "SELECT description, ai_query('gemini-3.8-flash', 'Classify sentiment in one word: ' || description) AS sentiment FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet') LIMIT 10",
    },
    {
        "name": "ai_summarize",
        "signature": "ai_summarize(text)",
        "description": "Generate a concise 1–2 sentence summary of unstructured text per row.",
        "example": "SELECT source, ai_summarize(content) AS brief FROM read_parquet('s3://datalake/embeddings/**/*.parquet') LIMIT 5",
    },
    {
        "name": "ai_classify",
        "signature": "ai_classify(text, categories[])",
        "description": "Zero-shot categorical classification into one of the provided array labels.",
        "example": "SELECT stage, ai_classify(stage, ['early_funnel', 'negotiation', 'closed']) AS deal_bucket FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet') LIMIT 10",
    },
    {
        "name": "ai_extract",
        "signature": "ai_extract(text, fields[])",
        "description": "Extract structured JSON object with the specified keys from unstructured text.",
        "example": "SELECT ai_extract(content, ['primary_metric', 'status', 'anomaly']) AS extracted_json FROM read_parquet('s3://datalake/embeddings/**/*.parquet') LIMIT 5",
    },
]

LEGACY_EMBEDDING_MODEL = "gemini-embedding-001"


def vector_search_sql(dimension: int, has_model_column: bool) -> str:
    """Nearest-neighbour SQL over ``embeddings/**`` for one embedding dimension (and model).

    Parameters, in order: query vector, parquet path, model name (only if ``has_model_column``),
    row limit. Files written before models were recorded have no ``model`` column and count as
    ``LEGACY_EMBEDDING_MODEL``. Keep identical to ``pipeline/shared/ai_gateway.py`` (a test checks).
    """
    dim = int(dimension)
    model_filter = (
        f"AND coalesce(model, '{LEGACY_EMBEDDING_MODEL}') = ?" if has_model_column else ""
    )
    return f"""
        SELECT source, ingested_at, content,
               CASE WHEN len(embedding) = {dim}
                    THEN array_cosine_similarity(embedding::DOUBLE[{dim}], ?::DOUBLE[{dim}])
               END AS score
        FROM read_parquet(?, union_by_name = true)
        WHERE len(embedding) = {dim} {model_filter}
        ORDER BY score DESC
        LIMIT ?
    """


_MODEL_NAME_RE = re.compile(r"^(models/)?(gemini|gemma|learnlm)-[a-z0-9._-]+$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_SQL_RE = re.compile(
    r"^\s*(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|COPY|ATTACH|DETACH|INSTALL|LOAD|PRAGMA|CALL|EXPORT|IMPORT|VACUUM)\b",
    re.IGNORECASE,
)


def clear_ai_cache() -> None:
    """Clear the in-memory LRU cache for SQL AI UDF responses."""
    with _AI_CACHE_LOCK:
        _AI_CACHE.clear()
    clear_model_cache()


def _cache_get(key: tuple[str, str, str]) -> str | None:
    with _AI_CACHE_LOCK:
        if key in _AI_CACHE:
            _AI_CACHE.move_to_end(key)
            return _AI_CACHE[key]
    return None


def _cache_set(key: tuple[str, str, str], value: str) -> None:
    with _AI_CACHE_LOCK:
        _AI_CACHE[key] = value
        _AI_CACHE.move_to_end(key)
        while len(_AI_CACHE) > AI_CACHE_MAX_SIZE:
            _AI_CACHE.popitem(last=False)


def _is_model_identifier(text: str) -> bool:
    s = text.strip()
    if not s or " " in s or len(s) > 64:
        return False
    return bool(_MODEL_NAME_RE.match(s))


def resolve_model_and_prompt(
    arg1: str, arg2: str | None = None, default_model: str | None = None
) -> tuple[str, str]:
    """Resolve (model, prompt) from 1-arg or 2-arg ai_query calls.

    Supports:
    - ai_query(prompt)
    - ai_query(model, prompt)  [Databricks standard]
    - ai_query(prompt, model)  [Alternative 2-arg order]
    """
    default_model = default_model or get_default_model()
    if arg2 is None:
        return default_model, str(arg1)

    s1 = str(arg1).strip()
    s2 = str(arg2).strip()
    if _is_model_identifier(s1) and not _is_model_identifier(s2):
        return s1, str(arg2)
    if _is_model_identifier(s2) and not _is_model_identifier(s1):
        return s2, str(arg1)
    # Default to Databricks order: (model, prompt)
    return s1 or default_model, str(arg2)


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _normalize_extracted_json(raw_text: str, fields: list[str]) -> str:
    """Ensure ai_extract returns a valid compact JSON object containing all requested fields."""
    cleaned = _strip_code_fences(raw_text)
    parsed: dict[str, Any] = {}
    try:
        candidate = json.loads(cleaned)
        if isinstance(candidate, dict):
            parsed = candidate
    except Exception:
        # Try finding first {...} block
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    parsed = candidate
            except Exception:
                pass

    normalized = {f: parsed.get(f, None) for f in fields}
    return json.dumps(normalized, ensure_ascii=False)


def _normalize_classification(raw_text: str, categories: list[str]) -> str:
    """Match LLM output to the closest canonical category label."""
    cleaned = _strip_code_fences(raw_text).strip().strip("\"'`")
    lower_map = {c.lower(): c for c in categories}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()]
    for cat in categories:
        if cat.lower() in cleaned.lower():
            return cat
    return cleaned


def _call_gemini_text(
    prompt: str,
    model: str | None = None,
    api_key: str | None = None,
    max_retries: int = 2,
) -> str:
    model = model or get_default_model()
    key = _resolve_api_key(api_key)
    if not key:
        raise RuntimeError(GEMINI_KEY_NOT_CONFIGURED_MSG)

    try:
        client = _client_for(key)
    except ProviderNotConfigured as exc:
        raise RuntimeError(GEMINI_KEY_NOT_CONFIGURED_MSG) from exc
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
            )
            return (resp.text or "").strip()
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
            if attempt < max_retries and (
                "429" in err_str or "rate" in err_str or "quota" in err_str
            ):
                time.sleep(0.25 * (2**attempt))
                continue
            raise
    if last_exc:
        raise last_exc
    return ""


def _execute_batch_prompts(
    tasks: list[tuple[str, str, str]],
    api_key: str | None,
    max_workers: int,
    state: dict[str, int],
) -> dict[tuple[str, str, str], str]:
    """Execute deduplicated (fn_name, model, prompt) tasks with LRU caching and ThreadPool concurrency."""
    results: dict[tuple[str, str, str], str] = {}
    uncached: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in tasks:
        if key in seen:
            continue
        seen.add(key)
        cached = _cache_get(key)
        if cached is not None:
            results[key] = cached
        else:
            uncached.append(key)

    if not uncached:
        return results

    effective_key = _resolve_api_key(api_key)
    if not effective_key:
        raise RuntimeError(GEMINI_KEY_NOT_CONFIGURED_MSG)

    def _worker(item: tuple[str, str, str]) -> tuple[tuple[str, str, str], str]:
        _, model, prompt = item
        text = _call_gemini_text(prompt=prompt, model=model, api_key=effective_key)
        return item, text

    workers = max(1, min(max_workers, len(uncached)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, output in pool.map(_worker, uncached):
            _cache_set(item, output)
            results[item] = output
            state["ai_calls"] = state.get("ai_calls", 0) + 1

    return results


def register_ai_functions(
    con: duckdb.DuckDBPyConnection,
    api_key: str | None = None,
    max_rows: int = AI_MAX_ROWS_PER_QUERY,
    max_workers: int = AI_MAX_CONCURRENCY,
    state: dict[str, int] | None = None,
) -> dict[str, int]:
    """Register Databricks-style SQL AI scalar/vectorized functions on a DuckDB connection.

    Functions registered:
    - ai_query([model,] prompt) -> VARCHAR
    - ai_summarize(text) -> VARCHAR
    - ai_classify(text, categories[]) -> VARCHAR
    - ai_extract(text, fields[]) -> VARCHAR (JSON object string)
    """
    if state is None:
        state = {"rows_processed": 0, "ai_calls": 0}

    def _check_and_increment_rows(count: int) -> None:
        state["rows_processed"] = state.get("rows_processed", 0) + count
        if state["rows_processed"] > max_rows:
            raise RuntimeError(
                f"AI function row limit exceeded ({max_rows} rows max per query). "
                "Add a LIMIT clause or narrow your filter."
            )

    def _ai_query_arrow(*args: Any) -> pa.Array:
        if len(args) not in (1, 2):
            raise ValueError(
                "ai_query expects 1 or 2 arguments: ai_query(prompt) or ai_query(model, prompt)"
            )
        cols = [a.to_pylist() for a in args]
        row_items: list[tuple[str, str, str] | None] = []
        valid_count = 0

        for row in zip(*cols):
            if any(v is None for v in row):
                row_items.append(None)
                continue
            if len(row) == 1:
                model, prompt = resolve_model_and_prompt(str(row[0]), None)
            else:
                model, prompt = resolve_model_and_prompt(str(row[0]), str(row[1]))
            valid_count += 1
            row_items.append(("ai_query", model, prompt))

        if valid_count > 0:
            _check_and_increment_rows(valid_count)
            batch_map = _execute_batch_prompts(
                [item for item in row_items if item is not None],
                api_key=api_key,
                max_workers=max_workers,
                state=state,
            )
        else:
            batch_map = {}

        out = [None if item is None else batch_map[item] for item in row_items]
        return pa.array(out, type=pa.string())

    def _ai_summarize_arrow(text_col: Any) -> pa.Array:
        texts = text_col.to_pylist()
        row_items: list[tuple[str, str, str] | None] = []
        valid_count = 0

        for t in texts:
            if t is None:
                row_items.append(None)
                continue
            s = str(t).strip()
            if not s:
                row_items.append(None)
                continue
            valid_count += 1
            prompt = (
                "Summarize the following text concisely in 1 to 2 sentences. "
                "Return only the summary without preamble:\n\n"
                f"{s}"
            )
            row_items.append(("ai_summarize", get_default_model(), prompt))

        if valid_count > 0:
            _check_and_increment_rows(valid_count)
            batch_map = _execute_batch_prompts(
                [item for item in row_items if item is not None],
                api_key=api_key,
                max_workers=max_workers,
                state=state,
            )
        else:
            batch_map = {}

        out = [None if item is None else batch_map[item] for item in row_items]
        return pa.array(out, type=pa.string())

    def _ai_classify_arrow(text_col: Any, categories_col: Any) -> pa.Array:
        texts = text_col.to_pylist()
        cats_list = categories_col.to_pylist()
        row_meta: list[tuple[tuple[str, str, str], list[str]] | None] = []
        valid_count = 0

        for t, cats in zip(texts, cats_list):
            if t is None or cats is None:
                row_meta.append(None)
                continue
            clean_cats = [str(c).strip() for c in cats if c is not None and str(c).strip()]
            if not clean_cats:
                row_meta.append(None)
                continue
            valid_count += 1
            cats_joined = ", ".join(f"'{c}'" for c in clean_cats)
            prompt = (
                f"Classify the following text into strictly one of these categories: [{cats_joined}]. "
                "Respond with ONLY the exact category label and nothing else.\n\n"
                f"Text: {t}"
            )
            key = ("ai_classify", get_default_model(), prompt)
            row_meta.append((key, clean_cats))

        if valid_count > 0:
            _check_and_increment_rows(valid_count)
            batch_map = _execute_batch_prompts(
                [m[0] for m in row_meta if m is not None],
                api_key=api_key,
                max_workers=max_workers,
                state=state,
            )
        else:
            batch_map = {}

        out: list[str | None] = []
        for m in row_meta:
            if m is None:
                out.append(None)
            else:
                key, clean_cats = m
                out.append(_normalize_classification(batch_map[key], clean_cats))
        return pa.array(out, type=pa.string())

    def _ai_extract_arrow(text_col: Any, fields_col: Any) -> pa.Array:
        texts = text_col.to_pylist()
        fields_list = fields_col.to_pylist()
        row_meta: list[tuple[tuple[str, str, str], list[str]] | None] = []
        valid_count = 0

        for t, fields in zip(texts, fields_list):
            if t is None or fields is None:
                row_meta.append(None)
                continue
            clean_fields = [str(f).strip() for f in fields if f is not None and str(f).strip()]
            if not clean_fields:
                row_meta.append(None)
                continue
            valid_count += 1
            fields_joined = ", ".join(f'"{f}"' for f in clean_fields)
            prompt = (
                f"Extract the following fields from the text as a JSON object with keys [{fields_joined}]. "
                "If a field is not present, set its value to null. "
                "Respond with ONLY valid JSON and no markdown formatting.\n\n"
                f"Text: {t}"
            )
            key = ("ai_extract", get_default_model(), prompt)
            row_meta.append((key, clean_fields))

        if valid_count > 0:
            _check_and_increment_rows(valid_count)
            batch_map = _execute_batch_prompts(
                [m[0] for m in row_meta if m is not None],
                api_key=api_key,
                max_workers=max_workers,
                state=state,
            )
        else:
            batch_map = {}

        out: list[str | None] = []
        for m in row_meta:
            if m is None:
                out.append(None)
            else:
                key, clean_fields = m
                out.append(_normalize_extracted_json(batch_map[key], clean_fields))
        return pa.array(out, type=pa.string())

    con.create_function(
        "ai_query",
        _ai_query_arrow,
        return_type="VARCHAR",
        type="arrow",
        null_handling="special",
        side_effects=True,
    )
    con.create_function(
        "ai_summarize",
        _ai_summarize_arrow,
        ["VARCHAR"],
        "VARCHAR",
        type="arrow",
        null_handling="special",
        side_effects=True,
    )
    con.create_function(
        "ai_classify",
        _ai_classify_arrow,
        ["VARCHAR", "VARCHAR[]"],
        "VARCHAR",
        type="arrow",
        null_handling="special",
        side_effects=True,
    )
    con.create_function(
        "ai_extract",
        _ai_extract_arrow,
        ["VARCHAR", "VARCHAR[]"],
        "VARCHAR",
        type="arrow",
        null_handling="special",
        side_effects=True,
    )
    return state


def _parse_s3_endpoint_and_ssl(
    raw_endpoint: str | None = None,
) -> tuple[str, bool]:
    raw = (raw_endpoint or os.getenv("MINIO_ENDPOINT", "localhost:9000")).strip()
    derived_ssl = False
    clean_endpoint = raw
    if "://" in raw:
        parsed = urlparse(raw)
        derived_ssl = parsed.scheme.lower() == "https"
        clean_endpoint = parsed.netloc or parsed.path
    elif raw.startswith("//"):
        parsed = urlparse("http:" + raw)
        clean_endpoint = parsed.netloc

    env_secure = os.getenv("MINIO_SECURE") or os.getenv("MINIO_USE_SSL") or os.getenv("S3_USE_SSL")
    if env_secure is not None:
        use_ssl = env_secure.strip().lower() in ("true", "1", "yes")
    else:
        use_ssl = derived_ssl

    clean_endpoint = clean_endpoint.split("/")[0]
    return clean_endpoint, use_ssl


def create_duckdb_connection(
    load_vss: bool = False,
    register_ai: bool = True,
    api_key: str | None = None,
    max_ai_rows: int = AI_MAX_ROWS_PER_QUERY,
    ai_state: dict[str, int] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Create an in-process DuckDB connection configured for MinIO S3 and optional AI UDFs."""
    s3_endpoint, s3_ssl = _parse_s3_endpoint_and_ssl(MINIO_ENDPOINT)
    config = {
        "s3_endpoint": s3_endpoint,
        "s3_access_key_id": MINIO_ACCESS_KEY,
        "s3_secret_access_key": MINIO_SECRET_KEY,
        "s3_use_ssl": s3_ssl,
        "s3_url_style": os.getenv("S3_URL_STYLE", "path"),
        "s3_region": os.getenv("AWS_REGION") or os.getenv("MINIO_REGION", "us-east-1"),
        "enable_object_cache": True,
    }
    con = duckdb.connect(config=config)
    try:
        con.execute("LOAD httpfs;")
    except Exception:
        try:
            con.execute("INSTALL httpfs; LOAD httpfs;")
        except Exception as exc:
            logger.warning("Failed to install/load httpfs: %s", exc)

    if load_vss:
        try:
            con.execute("LOAD vss;")
        except Exception:
            try:
                con.execute("INSTALL vss; LOAD vss;")
            except Exception as exc:
                logger.warning("Failed to install/load vss: %s", exc)

    if register_ai:
        register_ai_functions(
            con,
            api_key=api_key,
            max_rows=max_ai_rows,
            state=ai_state,
        )
    return con


def _safe_identifier(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def register_catalog_views(
    con: duckdb.DuckDBPyConnection,
    catalogs: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Register workspace Catalog tables as DuckDB schemas/views when S3 paths are available.

    Returns list of registered view names (e.g. 'bronze.stripe_charges').
    """
    registered: list[str] = []
    if not catalogs:
        return registered

    for cat in catalogs:
        for schema in cat.get("schemas") or []:
            schema_slug = _safe_identifier(schema.get("slug") or schema.get("name") or "default")
            try:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_slug}"')
            except Exception:
                continue

            for table in schema.get("tables") or []:
                table_slug = _safe_identifier(table.get("slug") or table.get("name") or "")
                s3_path = (table.get("s3_path_pattern") or "").strip()
                if not table_slug or not s3_path:
                    continue
                # Escape single quotes in S3 path
                escaped_path = s3_path.replace("'", "''")
                try:
                    con.execute(
                        f'CREATE OR REPLACE VIEW "{schema_slug}"."{table_slug}" AS '
                        f"SELECT * FROM read_parquet('{escaped_path}')"
                    )
                    registered.append(f"{schema_slug}.{table_slug}")
                except Exception:
                    # View creation may fail if S3 bucket/files do not exist yet; ignore safely
                    pass
    return registered


def get_workspace_catalogs_from_db(db: Session, workspace_id: Any) -> list[dict[str, Any]]:
    """Query active owned and approved shared catalogs with schemas and tables for a workspace."""
    from models import Catalog, CatalogAccess

    shared_ids = db.query(CatalogAccess.catalog_id).filter(
        CatalogAccess.workspace_id == workspace_id,
        CatalogAccess.status == "approved",
        CatalogAccess.suspended.is_(False),
    )
    catalogs = (
        db.query(Catalog)
        .filter(
            or_(Catalog.owner_workspace_id == workspace_id, Catalog.id.in_(shared_ids)),
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.created_at)
        .all()
    )

    result: list[dict[str, Any]] = []
    for c in catalogs:
        schemas_list: list[dict[str, Any]] = []
        for s in c.schemas:
            tables_list: list[dict[str, Any]] = []
            for t in s.tables:
                tables_list.append(
                    {
                        "id": str(t.id),
                        "name": t.name,
                        "slug": t.slug,
                        "description": t.description,
                        "s3_path_pattern": t.s3_path_pattern
                        or f"s3://{MINIO_BUCKET}/raw/{t.slug}/**/*.parquet",
                        "column_defs": t.column_defs or [],
                    }
                )
            schemas_list.append(
                {
                    "id": str(s.id),
                    "name": s.name,
                    "slug": s.slug,
                    "tier": s.tier,
                    "description": s.description,
                    "tables": tables_list,
                }
            )
        result.append(
            {
                "id": str(c.id),
                "name": c.name,
                "slug": c.slug,
                "description": c.description,
                "is_default": c.is_default,
                "schemas": schemas_list,
            }
        )
    return result


def build_schema_prompt_context(catalogs: list[dict[str, Any]] | None = None) -> str:
    """Build a comprehensive schema context string for Gemini Text-to-SQL and Query Fixing."""
    lines: list[str] = ["### Workspace Catalog Tables"]
    has_catalog_tables = False

    if catalogs:
        for cat in catalogs:
            cat_slug = cat.get("slug") or cat.get("name") or "default"
            for schema in cat.get("schemas") or []:
                schema_slug = schema.get("slug") or schema.get("name") or "bronze"
                for table in schema.get("tables") or []:
                    has_catalog_tables = True
                    t_slug = table.get("slug") or table.get("name")
                    s3_path = (
                        table.get("s3_path_pattern")
                        or f"s3://{MINIO_BUCKET}/raw/{t_slug}/**/*.parquet"
                    )
                    cols = table.get("column_defs") or []
                    col_str = (
                        ", ".join(
                            f"{c.get('name')} ({c.get('type', 'VARCHAR')})"
                            for c in cols
                            if isinstance(c, dict) and c.get("name")
                        )
                        if cols
                        else "schema-on-read"
                    )
                    desc = f" — {table.get('description')}" if table.get("description") else ""
                    lines.append(
                        f"- `{cat_slug}.{schema_slug}.{t_slug}` (View: `{schema_slug}.{t_slug}` or `read_parquet('{s3_path}')`){desc}\n"
                        f"  Columns: {col_str}"
                    )

    if not has_catalog_tables:
        lines.append("- (No custom tables registered in catalog yet; use S3 Parquet paths below)")

    lines.append("\n### Default Lakehouse S3 Parquet Datasets")
    for ds in DEFAULT_LAKEHOUSE_DATASETS:
        col_str = ", ".join(f"{c['name']} ({c['type']})" for c in ds["columns"])
        lines.append(
            f"- `{ds['name']}`: `read_parquet('{ds['s3_path']}')` — {ds['description']}\n"
            f"  Columns: {col_str}"
        )

    lines.append("\n### Built-in DuckDB SQL AI Functions")
    for fn in SQL_AI_FUNCTIONS:
        lines.append(f"- `{fn['signature']}`: {fn['description']}\n  Example: `{fn['example']}`")

    return "\n".join(lines)


def is_read_only_sql(sql: str) -> tuple[bool, str | None]:
    """Validate that a SQL query is read-only (SELECT, WITH, DESCRIBE, SUMMARIZE, SHOW, EXPLAIN)."""
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        return False, "SQL query cannot be empty."

    statements = [s.strip() for s in cleaned.split(";") if s.strip()]
    if len(statements) > 1:
        return False, "Only a single read-only SQL statement is permitted."

    stmt = statements[0]
    if _FORBIDDEN_SQL_RE.match(stmt):
        return (
            False,
            "Only read-only queries (SELECT, WITH, DESCRIBE, SUMMARIZE, SHOW) are allowed.",
        )

    if not re.match(r"^\s*(SELECT|WITH|DESCRIBE|SUMMARIZE|SHOW|EXPLAIN|VALUES)\b", stmt, re.I):
        return (
            False,
            "Query must begin with SELECT, WITH, DESCRIBE, SUMMARIZE, SHOW, or EXPLAIN.",
        )

    return True, None


def _serialize_cell(val: Any) -> Any:
    if val is None or isinstance(val, int | float | str | bool):
        return val
    from decimal import Decimal

    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, list | tuple):
        return [_serialize_cell(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _serialize_cell(v) for k, v in val.items()}
    return str(val)


def execute_sql_query(
    sql: str,
    catalogs: list[dict[str, Any]] | None = None,
    read_only: bool = False,
    max_ai_rows: int = AI_MAX_ROWS_PER_QUERY,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute a DuckDB SQL query with registered workspace Catalog views and SQL AI UDFs."""
    if read_only:
        allowed, reason = is_read_only_sql(sql)
        if not allowed:
            return {
                "ok": False,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "execution_ms": 0.0,
                "ai_calls_made": 0,
                "registered_views": [],
                "error": reason,
            }

    ai_state = {"rows_processed": 0, "ai_calls": 0}
    start_ts = time.perf_counter()
    con = None
    try:
        con = create_duckdb_connection(
            load_vss=False,
            register_ai=True,
            api_key=api_key,
            max_ai_rows=max_ai_rows,
            ai_state=ai_state,
        )
        registered_views = register_catalog_views(con, catalogs)
        rel = con.execute(sql)
        columns = [desc[0] for desc in (rel.description or [])]
        raw_rows = rel.fetchall()
        rows = [[_serialize_cell(cell) for cell in r] for r in raw_rows]
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        return {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "execution_ms": elapsed_ms,
            "ai_calls_made": ai_state.get("ai_calls", 0),
            "registered_views": registered_views,
            "error": None,
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        return {
            "ok": False,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "execution_ms": elapsed_ms,
            "ai_calls_made": ai_state.get("ai_calls", 0),
            "registered_views": [],
            "error": str(exc),
        }
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _parse_json_response(raw_text: str) -> dict[str, Any]:
    cleaned = _strip_code_fences(raw_text)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def generate_sql_from_prompt(
    prompt: str,
    catalogs: list[dict[str, Any]] | None = None,
    current_sql: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Generate executable DuckDB SQL from natural language using workspace Catalog schemas."""
    effective_key = _resolve_api_key(api_key)
    if not effective_key:
        return {"ok": False, "error": GEMINI_KEY_NOT_CONFIGURED_MSG}

    schema_ctx = build_schema_prompt_context(catalogs)
    current_ctx = f"\nCurrent SQL in editor:\n```sql\n{current_sql}\n```\n" if current_sql else ""

    full_prompt = (
        "You are an expert DuckDB SQL assistant for the Lake of Tears datalakehouse.\n"
        "Convert the user's natural language request into a clean, executable DuckDB SQL query.\n"
        "Prefer `read_parquet('s3://...')` paths unless a registered catalog view explicitly exists.\n"
        "When the user asks for sentiment analysis, summarization, classification, or extraction across rows, "
        "use the built-in DuckDB AI functions (`ai_query`, `ai_summarize`, `ai_classify`, `ai_extract`) and include a reasonable LIMIT (e.g. LIMIT 20).\n\n"
        f"{schema_ctx}\n"
        f"{current_ctx}\n"
        f"User Request: {prompt}\n\n"
        "Respond ONLY with a valid JSON object matching this exact schema:\n"
        '{"sql": "SELECT ...", "explanation": "Brief 1-2 sentence explanation", '
        '"tables_used": ["stripe_charges"], "ai_functions_used": ["ai_query"]}'
    )

    try:
        raw = _call_gemini_text(full_prompt, model=model, api_key=effective_key)
        parsed = _parse_json_response(raw)
        sql = _strip_code_fences(str(parsed.get("sql") or raw)).strip()
        explanation = str(
            parsed.get("explanation") or "Generated DuckDB SQL query from workspace schema."
        )
        tables_used = [str(t) for t in (parsed.get("tables_used") or []) if t]
        ai_fns = [str(f) for f in (parsed.get("ai_functions_used") or []) if f]
        if not ai_fns:
            for fn in ("ai_query", "ai_summarize", "ai_classify", "ai_extract"):
                if fn in sql:
                    ai_fns.append(fn)
        return {
            "ok": True,
            "sql": sql,
            "explanation": explanation,
            "tables_used": tables_used,
            "ai_functions_used": ai_fns,
            "error": None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def fix_sql_query(
    sql: str,
    error_message: str,
    catalogs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Diagnose and fix a failing DuckDB SQL query using the error message and Catalog schemas."""
    effective_key = _resolve_api_key(api_key)
    if not effective_key:
        return {"ok": False, "error": GEMINI_KEY_NOT_CONFIGURED_MSG}

    schema_ctx = build_schema_prompt_context(catalogs)
    full_prompt = (
        "You are an expert DuckDB SQL debugger for the Lake of Tears datalakehouse.\n"
        "Fix the broken SQL query below based on the DuckDB error message and available workspace schema.\n\n"
        f"{schema_ctx}\n\n"
        f"Broken SQL:\n```sql\n{sql}\n```\n\n"
        f"DuckDB Error:\n{error_message}\n\n"
        "Respond ONLY with a valid JSON object matching this exact schema:\n"
        '{"fixed_sql": "SELECT ...", "explanation": "What caused the error and how it was fixed"}'
    )

    try:
        raw = _call_gemini_text(full_prompt, model=model, api_key=effective_key)
        parsed = _parse_json_response(raw)
        fixed_sql = _strip_code_fences(str(parsed.get("fixed_sql") or raw)).strip()
        explanation = str(
            parsed.get("explanation") or "Corrected syntax and column references for DuckDB."
        )
        return {
            "ok": True,
            "fixed_sql": fixed_sql,
            "explanation": explanation,
            "error": None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def explain_sql_query(
    sql: str,
    catalogs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Explain a DuckDB SQL query step-by-step."""
    effective_key = _resolve_api_key(api_key)
    if not effective_key:
        return {"ok": False, "error": GEMINI_KEY_NOT_CONFIGURED_MSG}

    schema_ctx = build_schema_prompt_context(catalogs)
    full_prompt = (
        "You are an expert data analyst explaining a DuckDB SQL query in Lake of Tears.\n"
        "Break down what the query does clearly and concisely.\n\n"
        f"{schema_ctx}\n\n"
        f"SQL Query:\n```sql\n{sql}\n```\n\n"
        "Respond ONLY with a valid JSON object matching this exact schema:\n"
        '{"summary": "High-level 1-2 sentence overview", '
        '"steps": ["Step 1...", "Step 2..."], '
        '"tables_referenced": ["stripe_charges"], '
        '"ai_functions_used": ["ai_query"]}'
    )

    try:
        raw = _call_gemini_text(full_prompt, model=model, api_key=effective_key)
        parsed = _parse_json_response(raw)
        summary = str(parsed.get("summary") or raw)
        steps = [str(s) for s in (parsed.get("steps") or []) if s]
        tables_ref = [str(t) for t in (parsed.get("tables_referenced") or []) if t]
        ai_fns = [str(f) for f in (parsed.get("ai_functions_used") or []) if f]
        if not ai_fns:
            for fn in ("ai_query", "ai_summarize", "ai_classify", "ai_extract"):
                if fn in sql:
                    ai_fns.append(fn)
        return {
            "ok": True,
            "summary": summary,
            "steps": steps,
            "tables_referenced": tables_ref,
            "ai_functions_used": ai_fns,
            "error": None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def infer_chart_config(
    columns: list[str],
    rows: list[list[Any]],
    question: str = "",
) -> dict[str, Any]:
    """Infer an Apache ECharts visualization configuration from tabular query results."""
    if not columns or not rows:
        return {"type": "table", "title": "Query Results", "x_column": None, "y_columns": []}

    if len(rows) == 1 and len(columns) <= 3:
        numeric_cols = [
            col for idx, col in enumerate(columns) if isinstance(rows[0][idx], int | float)
        ]
        if numeric_cols:
            return {
                "type": "kpi",
                "title": question[:60] or "Key Metric",
                "x_column": columns[0],
                "y_columns": numeric_cols,
            }

    x_col = columns[0]
    numeric_cols = []
    for idx, col in enumerate(columns):
        if idx == 0 and len(columns) > 1:
            continue
        sample_vals = [r[idx] for r in rows[:10] if idx < len(r) and r[idx] is not None]
        if sample_vals and all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in sample_vals
        ):
            numeric_cols.append(col)

    if not numeric_cols:
        return {
            "type": "table",
            "title": question[:60] or "Query Results",
            "x_column": x_col,
            "y_columns": [],
        }

    q_lower = question.lower()
    x_lower = x_col.lower()
    is_time = any(
        k in x_lower or k in q_lower
        for k in ("date", "day", "month", "year", "time", "week", "trend", "daily", "monthly")
    )
    if "pie" in q_lower or "share" in q_lower or "breakdown" in q_lower and len(rows) <= 8:
        chart_type = "pie"
    elif is_time:
        chart_type = "line"
    else:
        chart_type = "bar"

    return {
        "type": chart_type,
        "title": question[:60] or f"{', '.join(numeric_cols[:2])} by {x_col}",
        "x_column": x_col,
        "y_columns": numeric_cols[:3],
    }


def run_rag_search(question: str, api_key: str | None = None) -> dict[str, Any]:
    """Execute semantic vector search over embeddings/**/*.parquet and synthesize a grounded answer."""
    effective_key = _resolve_api_key(api_key)
    if not effective_key:
        return {"ok": False, "error": GEMINI_KEY_NOT_CONFIGURED_MSG, "snippets": []}
    try:
        client = _client_for(effective_key)
        # The active provider decides the embedding model and dimension; only stored vectors made
        # by the same model (and of the same length) are compared.
        embed_resp = client.models.embed_content(
            contents=question, config={"task_type": "RETRIEVAL_QUERY"}
        )
        q_vec = embed_resp.embeddings[0].values

        s3_pattern = f"s3://{MINIO_BUCKET}/embeddings/**/*.parquet"
        con = create_duckdb_connection(register_ai=False)
        try:
            columns = {
                d[0]
                for d in con.execute(
                    "SELECT * FROM read_parquet(?, union_by_name = true) LIMIT 0", [s3_pattern]
                ).description
            }
            has_model = "model" in columns
            params = [q_vec, s3_pattern] + ([embed_resp.model] if has_model else []) + [5]
            rows = con.execute(
                vector_search_sql(embed_resp.dimension, has_model), params
            ).fetchall()
        finally:
            con.close()

        snippets = [
            {
                "source": r[0],
                "ingested_at": str(r[1]),
                "summary": r[2],
                "score": round(float(r[3]), 4),
            }
            for r in rows
        ]
        context_text = "\n\n".join(
            f"[{s['source']} @ {s['ingested_at']} (score={s['score']})]:\n{s['summary']}"
            for s in snippets
        )
        prompt = (
            "You are an analyst for a business datalakehouse called Lake of Tears. "
            "Answer the following question using the context snippets below. "
            "Be concise and cite which data sources support your answer.\n\n"
            f"Question: {question}\n\nContext:\n{context_text}"
        )
        answer = client.models.generate_content(contents=prompt).text
        return {"ok": True, "answer": (answer or "").strip(), "snippets": snippets}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "snippets": []}


def run_genie_chat(
    question: str,
    mode: str = "genie",
    history: list[dict[str, Any]] | None = None,
    catalogs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute the Databricks Genie-style conversational data analyst loop:
    Question -> Schema Discovery -> SQL Synthesis -> DuckDB Execution (with 1-step self-correction) ->
    Chart Inference + Grounded Narrative Insight.
    """
    effective_key = _resolve_api_key(api_key)
    if not effective_key:
        return {"ok": False, "mode": mode, "error": GEMINI_KEY_NOT_CONFIGURED_MSG}

    if mode == "rag":
        rag_res = run_rag_search(question, api_key=effective_key)
        if not rag_res.get("ok"):
            return {"ok": False, "mode": "rag", "error": rag_res.get("error")}
        return {
            "ok": True,
            "mode": "rag",
            "answer": rag_res.get("answer", ""),
            "sql": None,
            "self_corrected": False,
            "initial_error": None,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "execution_ms": 0.0,
            "chart_config": None,
            "snippets": rag_res.get("snippets", []),
            "tables_used": [],
            "error": None,
        }

    # Build multi-turn conversation history context
    history_lines: list[str] = []
    for turn in (history or [])[-6:]:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        turn_sql = turn.get("sql")
        if turn_sql:
            history_lines.append(f"{role.upper()}: {content}\nSQL: {turn_sql}")
        else:
            history_lines.append(f"{role.upper()}: {content}")
    history_ctx = (
        "\n### Prior Conversation Turns\n" + "\n".join(history_lines) + "\n"
        if history_lines
        else ""
    )

    schema_ctx = build_schema_prompt_context(catalogs)
    synth_prompt = (
        "You are the Lake of Tears Genie Data Analyst, an autonomous conversational SQL agent.\n"
        "Given the workspace catalog schema, conversation history, and the user's question, "
        "write a single read-only DuckDB SELECT query to answer the question.\n"
        "Rules:\n"
        "1. Only generate read-only SELECT / WITH queries (never DROP/INSERT/UPDATE/DELETE).\n"
        "2. Always include a LIMIT (e.g. LIMIT 50) unless aggregating to a small summary table.\n"
        "3. You may use DuckDB SQL AI functions (`ai_query`, `ai_summarize`, `ai_classify`, `ai_extract`) when appropriate.\n\n"
        f"{schema_ctx}\n"
        f"{history_ctx}\n"
        f"User Question: {question}\n\n"
        "Respond ONLY with a valid JSON object matching this exact schema:\n"
        '{"sql": "SELECT ...", "tables_used": ["stripe_charges"], "reasoning": "Brief plan"}'
    )

    try:
        raw_plan = _call_gemini_text(synth_prompt, model=model, api_key=effective_key)
        parsed_plan = _parse_json_response(raw_plan)
        generated_sql = _strip_code_fences(str(parsed_plan.get("sql") or raw_plan)).strip()
        tables_used = [str(t) for t in (parsed_plan.get("tables_used") or []) if t]
    except Exception as exc:
        return {"ok": False, "mode": mode, "error": f"Failed to synthesize SQL: {exc}"}

    # Execute generated SQL in DuckDB (read_only=True)
    exec_res = execute_sql_query(
        generated_sql,
        catalogs=catalogs,
        read_only=True,
        api_key=effective_key,
    )

    self_corrected = False
    initial_error: str | None = None

    # Automatic 1-step self-correction if DuckDB execution failed
    if not exec_res["ok"] and exec_res.get("error"):
        initial_error = exec_res["error"]
        fix_res = fix_sql_query(
            sql=generated_sql,
            error_message=initial_error or "",
            catalogs=catalogs,
            model=model,
            api_key=effective_key,
        )
        if fix_res.get("ok") and fix_res.get("fixed_sql"):
            candidate_sql = fix_res["fixed_sql"]
            retry_res = execute_sql_query(
                candidate_sql,
                catalogs=catalogs,
                read_only=True,
                api_key=effective_key,
            )
            if retry_res["ok"]:
                generated_sql = candidate_sql
                exec_res = retry_res
                self_corrected = True

    snippets: list[dict[str, Any]] = []
    if mode == "hybrid" or not exec_res["ok"]:
        rag_res = run_rag_search(question, api_key=effective_key)
        if rag_res.get("ok"):
            snippets = rag_res.get("snippets", [])

    columns = exec_res.get("columns", [])
    rows = exec_res.get("rows", [])
    row_count = exec_res.get("row_count", 0)
    execution_ms = exec_res.get("execution_ms", 0.0)
    chart_config = infer_chart_config(columns, rows, question=question) if exec_res["ok"] else None

    # Synthesize grounded narrative insight from SQL results (and optional RAG snippets)
    sample_rows = rows[:20]
    if exec_res["ok"]:
        insight_prompt = (
            "You are the Lake of Tears Genie Data Analyst.\n"
            "Provide a clear, concise, data-driven answer to the user's question based on the executed DuckDB query results.\n"
            "Highlight key numbers, trends, or top items.\n\n"
            f"Question: {question}\n"
            f"Executed SQL:\n```sql\n{generated_sql}\n```\n"
            f"Columns: {columns}\n"
            f"Sample Rows ({row_count} total): {json.dumps(sample_rows, default=str)}\n"
        )
        if snippets:
            snippet_text = "\n".join(f"- [{s['source']}]: {s['summary']}" for s in snippets[:3])
            insight_prompt += f"\nAdditional Semantic Context:\n{snippet_text}\n"
    else:
        # If SQL failed even after self-correction, still provide a helpful explanation or RAG fallback
        if snippets:
            snippet_text = "\n".join(f"- [{s['source']}]: {s['summary']}" for s in snippets)
            insight_prompt = (
                "You are the Lake of Tears Genie Data Analyst.\n"
                "The live SQL query could not be executed against the current S3 Parquet files "
                f"({exec_res.get('error')}), so answer using these semantic search snippets and explain the SQL query you synthesized:\n\n"
                f"Question: {question}\nSnippets:\n{snippet_text}"
            )
        else:
            return {
                "ok": True,
                "mode": mode,
                "answer": (
                    f"I synthesized a DuckDB query for your question, but execution returned an error: "
                    f"`{exec_res.get('error')}`. You can open the query in the SQL Editor to inspect or adjust the S3 path."
                ),
                "sql": generated_sql,
                "self_corrected": self_corrected,
                "initial_error": initial_error,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "execution_ms": execution_ms,
                "chart_config": None,
                "snippets": [],
                "tables_used": tables_used,
                "error": exec_res.get("error"),
            }

    try:
        answer = _call_gemini_text(insight_prompt, model=model, api_key=effective_key)
    except Exception as exc:
        answer = f"Query returned {row_count} rows. (Narrative synthesis error: {exc})"

    return {
        "ok": True,
        "mode": mode,
        "answer": answer,
        "sql": generated_sql,
        "self_corrected": self_corrected,
        "initial_error": initial_error,
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "execution_ms": execution_ms,
        "chart_config": chart_config,
        "snippets": snippets,
        "tables_used": tables_used,
        "error": None,
    }
