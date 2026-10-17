from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import weakref
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import duckdb
import pyarrow as pa
from google import genai

logger = logging.getLogger("lake.ui.ai")

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
_default_model_provider: Any = None


class GenieCancelled(Exception):
    """Raised when a user cancels an in-flight AI request."""


class CancelToken:
    """Cooperative cancellation handle for a single AI request.

    Call ``check()`` between steps to abort early. ``cancel()`` also interrupts any DuckDB
    connection attached via ``attach()`` so a running query stops immediately. An in-flight
    Gemini call cannot be killed; its result is discarded at the next ``check()``.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._con: Any = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise GenieCancelled()

    def attach(self, con: Any) -> None:
        with self._lock:
            self._con = con

    def detach(self) -> None:
        with self._lock:
            self._con = None

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            con = self._con
        if con is not None:
            try:
                con.interrupt()
            except Exception:
                pass


def set_default_model_provider(provider: Any) -> None:
    """Register a callable returning the Admin Panel default Gemini model."""
    global _default_model_provider
    _default_model_provider = provider


def get_default_model() -> str:
    """Default Gemini model from the Admin Panel setting, else the built-in default."""
    if callable(_default_model_provider):
        try:
            val = _default_model_provider()
            if val and str(val).strip():
                return str(val).strip()
        except Exception:
            pass
    return DEFAULT_GEMINI_MODEL


AI_MAX_ROWS_PER_QUERY = int(os.getenv("AI_MAX_ROWS_PER_QUERY", "50"))
AI_MAX_CONCURRENCY = int(os.getenv("AI_MAX_CONCURRENCY", "5"))
AI_CACHE_MAX_SIZE = 1024
GEMINI_KEY_NOT_CONFIGURED_MSG = (
    "GEMINI_API_KEY is not set. A superadmin can configure it in Settings → Admin Panel."
)


def _resolve_key(api_key: Any) -> str:
    if callable(api_key):
        try:
            val = api_key()
            return (val or "").strip()
        except Exception:
            return ""
    if isinstance(api_key, str):
        return api_key.strip()
    return ""


_AI_CACHE_LOCK = threading.Lock()
_AI_CACHE: OrderedDict[tuple[str, str, str], str] = OrderedDict()

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


def get_default_datasets(bucket: str = "datalake") -> list[dict[str, Any]]:
    return [
        {
            "name": "stripe_charges",
            "schema": "bronze",
            "s3_path": f"s3://{bucket}/raw/stripe_charges/**/*.parquet",
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
            "s3_path": f"s3://{bucket}/raw/shopify_line_items/**/*.parquet",
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
            "s3_path": f"s3://{bucket}/raw/hubspot_deals/**/*.parquet",
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
            "s3_path": f"s3://{bucket}/raw/weather/**/*.parquet",
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
            "s3_path": f"s3://{bucket}/embeddings/**/*.parquet",
            "description": "768-dim Gemini embeddings of each source's record text (content), written daily by the embed_sources pipeline.",
            "columns": [
                {"name": "id", "type": "VARCHAR"},
                {"name": "source", "type": "VARCHAR"},
                {"name": "content", "type": "VARCHAR"},
                {"name": "embedding", "type": "DOUBLE[]"},
                {"name": "source_date", "type": "VARCHAR"},
                {"name": "ingested_at", "type": "TIMESTAMP"},
            ],
        },
    ]


_MODEL_NAME_RE = re.compile(r"^(models/)?(gemini|gemma|learnlm)-[a-z0-9._-]+$", re.IGNORECASE)
_FORBIDDEN_SQL_RE = re.compile(
    r"^\s*(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|COPY|ATTACH|DETACH|INSTALL|LOAD|PRAGMA|CALL|EXPORT|IMPORT|VACUUM)\b",
    re.IGNORECASE,
)


def clear_ai_cache() -> None:
    with _AI_CACHE_LOCK:
        _AI_CACHE.clear()


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
    default_model = default_model or get_default_model()
    if arg2 is None:
        return default_model, str(arg1)

    s1 = str(arg1).strip()
    s2 = str(arg2).strip()
    if _is_model_identifier(s1) and not _is_model_identifier(s2):
        return s1, str(arg2)
    if _is_model_identifier(s2) and not _is_model_identifier(s1):
        return s2, str(arg1)
    return s1 or default_model, str(arg2)


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _normalize_extracted_json(raw_text: str, fields: list[str]) -> str:
    cleaned = _strip_code_fences(raw_text)
    parsed: dict[str, Any] = {}
    try:
        candidate = json.loads(cleaned)
        if isinstance(candidate, dict):
            parsed = candidate
    except Exception:
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
    cleaned = _strip_code_fences(raw_text).strip().strip("\"'`")
    lower_map = {c.lower(): c for c in categories}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()]
    for cat in categories:
        if cat.lower() in cleaned.lower():
            return cat
    return cleaned


def call_gemini_text(
    prompt: str,
    model: str | None = None,
    api_key: Any = None,
    client_factory: Any = None,
    max_retries: int = 2,
) -> str:
    model = model or get_default_model()
    key = _resolve_key(api_key)
    if not key:
        raise RuntimeError(GEMINI_KEY_NOT_CONFIGURED_MSG)

    factory = client_factory or genai.Client
    client = factory(api_key=key)
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
    api_key: Any,
    max_workers: int,
    state: dict[str, int],
    client_factory: Any = None,
) -> dict[tuple[str, str, str], str]:
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

    effective_key = _resolve_key(api_key)
    if not effective_key:
        raise RuntimeError(GEMINI_KEY_NOT_CONFIGURED_MSG)

    def _worker(item: tuple[str, str, str]) -> tuple[tuple[str, str, str], str]:
        _, model, prompt = item
        text = call_gemini_text(
            prompt=prompt,
            model=model,
            api_key=effective_key,
            client_factory=client_factory,
        )
        return item, text

    workers = max(1, min(max_workers, len(uncached)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, output in pool.map(_worker, uncached):
            _cache_set(item, output)
            results[item] = output
            state["ai_calls"] = state.get("ai_calls", 0) + 1

    return results


_con_ai_contexts: weakref.WeakKeyDictionary[duckdb.DuckDBPyConnection, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)


def register_ai_functions(
    con: duckdb.DuckDBPyConnection,
    api_key: str | None = None,
    max_rows: int = AI_MAX_ROWS_PER_QUERY,
    max_workers: int = AI_MAX_CONCURRENCY,
    state: dict[str, int] | None = None,
    client_factory: Any = None,
) -> dict[str, int]:
    """Register `ai_query`, `ai_summarize`, `ai_classify`, and `ai_extract` on a DuckDB connection."""
    if state is None:
        state = {"rows_processed": 0, "ai_calls": 0}

    if con in _con_ai_contexts:
        ctx = _con_ai_contexts[con]
        ctx["state"] = state
        if api_key is not None:
            ctx["api_key"] = api_key
        ctx["max_rows"] = max_rows
        ctx["max_workers"] = max_workers
        if client_factory is not None:
            ctx["client_factory"] = client_factory
        return state

    ctx = {
        "state": state,
        "api_key": api_key,
        "max_rows": max_rows,
        "max_workers": max_workers,
        "client_factory": client_factory,
    }
    _con_ai_contexts[con] = ctx

    def _get_ctx() -> dict[str, Any]:
        return _con_ai_contexts.get(con, ctx)

    def _check_and_increment_rows(count: int) -> None:
        cur_ctx = _get_ctx()
        cur_state = cur_ctx["state"]
        cur_max = cur_ctx["max_rows"]
        cur_state["rows_processed"] = cur_state.get("rows_processed", 0) + count
        if cur_state["rows_processed"] > cur_max:
            raise RuntimeError(
                f"AI function row limit exceeded ({cur_max} rows max per query). "
                "Add a LIMIT clause or narrow your filter."
            )

    def _ai_query_arrow(*args: Any) -> pa.Array:
        cur_ctx = _get_ctx()
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
                api_key=cur_ctx["api_key"],
                max_workers=cur_ctx["max_workers"],
                state=cur_ctx["state"],
                client_factory=cur_ctx["client_factory"],
            )
        else:
            batch_map = {}

        out = [None if item is None else batch_map[item] for item in row_items]
        return pa.array(out, type=pa.string())

    def _ai_summarize_arrow(text_col: Any) -> pa.Array:
        cur_ctx = _get_ctx()
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
                api_key=cur_ctx["api_key"],
                max_workers=cur_ctx["max_workers"],
                state=cur_ctx["state"],
                client_factory=cur_ctx["client_factory"],
            )
        else:
            batch_map = {}

        out = [None if item is None else batch_map[item] for item in row_items]
        return pa.array(out, type=pa.string())

    def _ai_classify_arrow(text_col: Any, categories_col: Any) -> pa.Array:
        cur_ctx = _get_ctx()
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
                api_key=cur_ctx["api_key"],
                max_workers=cur_ctx["max_workers"],
                state=cur_ctx["state"],
                client_factory=cur_ctx["client_factory"],
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
        cur_ctx = _get_ctx()
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
                api_key=cur_ctx["api_key"],
                max_workers=cur_ctx["max_workers"],
                state=cur_ctx["state"],
                client_factory=cur_ctx["client_factory"],
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


def _safe_identifier(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def register_catalog_views(
    con: duckdb.DuckDBPyConnection,
    catalogs: list[dict[str, Any]] | None = None,
) -> list[str]:
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
                escaped_path = s3_path.replace("'", "''")
                try:
                    con.execute(
                        f'CREATE OR REPLACE VIEW "{schema_slug}"."{table_slug}" AS '
                        f"SELECT * FROM read_parquet('{escaped_path}')"
                    )
                    registered.append(f"{schema_slug}.{table_slug}")
                except Exception:
                    pass
    return registered


def build_schema_prompt_context(
    catalogs: list[dict[str, Any]] | None = None,
    bucket: str = "datalake",
) -> str:
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
                        table.get("s3_path_pattern") or f"s3://{bucket}/raw/{t_slug}/**/*.parquet"
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
    for ds in get_default_datasets(bucket=bucket):
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
    api_key: Any = None,
    bucket: str = "datalake",
    client_factory: Any = None,
) -> dict[str, Any]:
    effective_key = _resolve_key(api_key)
    if not effective_key:
        return {
            "ok": False,
            "sql": "",
            "explanation": "",
            "tables_used": [],
            "ai_functions_used": [],
            "error": GEMINI_KEY_NOT_CONFIGURED_MSG,
        }

    schema_ctx = build_schema_prompt_context(catalogs, bucket=bucket)
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
        raw = call_gemini_text(
            full_prompt, model=model, api_key=effective_key, client_factory=client_factory
        )
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
        return {
            "ok": False,
            "sql": "",
            "explanation": "",
            "tables_used": [],
            "ai_functions_used": [],
            "error": str(exc),
        }


def fix_sql_query(
    sql: str,
    error_message: str,
    catalogs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    api_key: Any = None,
    bucket: str = "datalake",
    client_factory: Any = None,
) -> dict[str, Any]:
    effective_key = _resolve_key(api_key)
    if not effective_key:
        return {
            "ok": False,
            "fixed_sql": "",
            "explanation": "",
            "error": GEMINI_KEY_NOT_CONFIGURED_MSG,
        }

    schema_ctx = build_schema_prompt_context(catalogs, bucket=bucket)
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
        raw = call_gemini_text(
            full_prompt, model=model, api_key=effective_key, client_factory=client_factory
        )
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
        return {"ok": False, "fixed_sql": "", "explanation": "", "error": str(exc)}


def explain_sql_query(
    sql: str,
    catalogs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    api_key: Any = None,
    bucket: str = "datalake",
    client_factory: Any = None,
) -> dict[str, Any]:
    effective_key = _resolve_key(api_key)
    if not effective_key:
        return {
            "ok": False,
            "summary": "",
            "steps": [],
            "tables_referenced": [],
            "ai_functions_used": [],
            "error": GEMINI_KEY_NOT_CONFIGURED_MSG,
        }

    schema_ctx = build_schema_prompt_context(catalogs, bucket=bucket)
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
        raw = call_gemini_text(
            full_prompt, model=model, api_key=effective_key, client_factory=client_factory
        )
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
        return {
            "ok": False,
            "summary": "",
            "steps": [],
            "tables_referenced": [],
            "ai_functions_used": [],
            "error": str(exc),
        }


def serialize_cell(val: Any) -> Any:
    if val is None or isinstance(val, int | float | str | bool):
        return val
    from decimal import Decimal

    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, list | tuple):
        return [serialize_cell(v) for v in val]
    if isinstance(val, dict):
        return {str(k): serialize_cell(v) for k, v in val.items()}
    return str(val)


def infer_chart_config(
    columns: list[str],
    rows: list[list[Any] | tuple[Any, ...]],
    question: str = "",
) -> dict[str, Any]:
    from decimal import Decimal

    if not columns or not rows:
        return {"type": "table", "title": "Query Results", "x_column": None, "y_columns": []}

    if len(rows) == 1 and len(columns) <= 3:
        numeric_cols = [
            col
            for idx, col in enumerate(columns)
            if isinstance(rows[0][idx], int | float | Decimal)
            and not isinstance(rows[0][idx], bool)
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
            isinstance(v, int | float | Decimal) and not isinstance(v, bool) for v in sample_vals
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
    if ("pie" in q_lower or "share" in q_lower or "breakdown" in q_lower) and len(rows) <= 8:
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
