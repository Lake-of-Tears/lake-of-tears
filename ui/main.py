import asyncio
import json
import logging
import math
import os
import re
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions
import duckdb
import httpx
import jwt
import websockets
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Form,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

try:
    from ui import (
        ai_gateway_client,
        duckdb_ai,
        duckdb_pool,
        ingest_service,
        notebook_proxy,
        sql_guardrails,
    )
except ImportError:
    import ai_gateway_client
    import duckdb_ai
    import duckdb_pool
    import ingest_service
    import notebook_proxy
    import sql_guardrails

try:
    from pipeline.shared.connector_registry import registry as connector_registry
except ImportError:
    try:
        from ui.connector_registry import registry as connector_registry
    except ImportError:
        try:
            from connector_registry import registry as connector_registry
        except ImportError:
            import sys

            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from pipeline.shared.connector_registry import registry as connector_registry

logger = logging.getLogger("lake.ui")

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
AUTH_REGISTRATION_ENABLED = os.getenv("AUTH_REGISTRATION_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
    "off",
)
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
MINIO_CONSOLE_URL = os.getenv("MINIO_CONSOLE_URL", "http://localhost:9001")
SUPERSET_URL = os.getenv("SUPERSET_URL", "http://localhost:8088")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")

SOURCES = [
    "postgres",
    "mysql",
    "snowflake",
    "kafka",
    "s3",
    "stripe",
    "shopify",
    "hubspot",
    "salesforce",
    "google_analytics",
    "github",
    "webhook",
]
SOURCE_LABELS = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "snowflake": "Snowflake",
    "kafka": "Apache Kafka",
    "s3": "Amazon S3 / Storage",
    "stripe": "Stripe",
    "shopify": "Shopify",
    "hubspot": "HubSpot",
    "salesforce": "Salesforce",
    "google_analytics": "Google Analytics 4",
    "github": "GitHub",
    "webhook": "REST Webhook",
}

app = FastAPI(title="Lake of Tears")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_SCHEMA_TIER_ORDER = {"bronze": 1, "silver": 2, "gold": 3}


def _safe_identifier(name: str | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip())
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def _sort_schemas(schemas: Any) -> list[dict[str, Any]]:
    if not isinstance(schemas, list):
        return []

    def _key(s: Any) -> tuple[int, str]:
        tier = (
            getattr(s, "tier", None)
            or (s.get("tier") if isinstance(s, dict) else None)
            or getattr(s, "slug", None)
            or (s.get("slug") if isinstance(s, dict) else None)
            or ""
        ).lower()
        name = (
            getattr(s, "name", None)
            or (s.get("name") if isinstance(s, dict) else None)
            or getattr(s, "slug", None)
            or (s.get("slug") if isinstance(s, dict) else None)
            or ""
        ).lower()
        return (_SCHEMA_TIER_ORDER.get(tier, 99), name)

    return sorted(schemas, key=_key)


# Catalog slugs use hyphens but the DuckDB views use underscores; templates show the SQL name.
templates.env.filters["sql_ident"] = duckdb_ai._safe_identifier
templates.env.filters["sort_schemas"] = _sort_schemas
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_PUBLIC_PREFIXES = (
    "/login",
    "/logout",
    "/api/",
    "/health",
    "/workspace/search",
    "/catalog/preview",
)
_WORKSPACE_COOKIE = "lake_workspace_id"

_SESSION_CONFIG_CACHE: dict | None = None
_SESSION_CONFIG_FETCHED_AT: float = 0.0
_SESSION_CONFIG_TTL = 60.0  # seconds


def _get_gemini_default_model() -> str:
    """Chat model of the active AI provider (from the backend gateway), else the built-in default."""
    return ai_gateway_client.chat_model() or duckdb_ai.DEFAULT_GEMINI_MODEL


duckdb_ai.set_default_model_provider(_get_gemini_default_model)


def _get_ai_access() -> str:
    """Non-empty when an AI provider is configured in the Admin Panel, else an empty string.

    The UI holds no credentials: requests go through the backend AI gateway, so this is only an
    availability check (the value is a placeholder, never a key).
    """
    return "gateway" if ai_gateway_client.is_configured() else ""


def _ai_client(api_key: str | None = None) -> ai_gateway_client.GatewayClient:
    return ai_gateway_client.GatewayClient()


def _get_session_config() -> dict:
    global _SESSION_CONFIG_CACHE, _SESSION_CONFIG_FETCHED_AT
    now = time.monotonic()
    if _SESSION_CONFIG_CACHE is not None and now - _SESSION_CONFIG_FETCHED_AT < _SESSION_CONFIG_TTL:
        return _SESSION_CONFIG_CACHE
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.get(f"{BACKEND_URL}/api/admin/settings/public")
        if resp.status_code == 200:
            _SESSION_CONFIG_CACHE = resp.json()
            _SESSION_CONFIG_FETCHED_AT = now
            return _SESSION_CONFIG_CACHE
    except Exception:
        pass
    return {"session_inactivity_timeout_minutes": 0, "session_max_hours": 24}


def _fetch_workspaces(token: str) -> list[dict] | None:
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.get(
                f"{BACKEND_URL}/api/workspaces",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            return None
    except Exception:
        pass
    return []


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Defaults
        request.state.user = None
        request.state.workspace = None
        request.state.workspaces = []
        request.state.has_shared_access = False
        request.state.session_config = {
            "session_inactivity_timeout_minutes": 0,
            "session_max_hours": 24,
        }

        if not AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        is_public = any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)

        token = request.cookies.get("lake_token")
        if not token:
            if is_public:
                return await call_next(request)
            return RedirectResponse(url=f"/login?next={request.url}", status_code=302)

        try:
            payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
            request.state.user = payload
        except jwt.ExpiredSignatureError:
            if is_public:
                return await call_next(request)
            return RedirectResponse(url="/login?error=expired", status_code=302)
        except jwt.InvalidTokenError:
            if is_public:
                return await call_next(request)
            return RedirectResponse(url="/login?error=invalid", status_code=302)

        # Inject session config for the idle-timeout JS in base.html
        import asyncio

        loop = asyncio.get_event_loop()
        session_cfg = await loop.run_in_executor(None, _get_session_config)
        workspaces = await loop.run_in_executor(None, _fetch_workspaces, token)
        if workspaces is None:
            if is_public:
                return await call_next(request)
            resp = RedirectResponse(url="/login?reason=session_expired", status_code=302)
            resp.delete_cookie("lake_token")
            resp.delete_cookie(_WORKSPACE_COOKIE)
            return resp

        request.state.session_config = session_cfg
        request.state.workspaces = workspaces

        active_id = request.cookies.get(_WORKSPACE_COOKIE, "")
        active = next((w for w in workspaces if str(w["id"]) == active_id), None)
        if not active and workspaces:
            active = workspaces[0]
        request.state.workspace = active
        request.state.has_shared_access = bool(
            any(w.get("is_shared") for w in workspaces)
            or (active and active.get("has_shared_catalogs"))
        )

        response = await call_next(request)
        if active and (not active_id or active_id != str(active["id"])):
            response.set_cookie(
                _WORKSPACE_COOKIE,
                str(active["id"]),
                max_age=30 * 24 * 3600,
                samesite="lax",
            )
        return response


app.add_middleware(AuthMiddleware)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s3_client():
    clean_endpoint, use_ssl = duckdb_pool.parse_s3_endpoint_and_ssl(MINIO_ENDPOINT)
    scheme = "https" if use_ssl else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{clean_endpoint}",
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name=os.getenv("AWS_REGION", os.getenv("MINIO_REGION", "us-east-1")),
    )


def _human_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    exp = min(int(math.log(num_bytes, 1024)), len(units) - 1)
    return f"{num_bytes / (1024**exp):.1f} {units[exp]}"


def _source_status(last_modified: datetime | None) -> str:
    if last_modified is None:
        return "red"
    age = datetime.now(UTC) - last_modified
    if age <= timedelta(hours=25):
        return "green"
    if age <= timedelta(hours=48):
        return "yellow"
    return "red"


def _get_bucket_stats(workspace_slug: str | None = None) -> dict[str, Any]:
    try:
        s3 = _s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        total_objects = total_size = 0
        prefix_stats: dict[str, dict] = {}
        source_last_modified: dict[str, datetime | None] = {s: None for s in SOURCES}

        clean_slug = (workspace_slug or "").strip("/")
        ws_root = f"workspaces/{clean_slug}/" if clean_slug else ""

        paginate_kwargs: dict[str, Any] = {"Bucket": MINIO_BUCKET}
        if ws_root:
            paginate_kwargs["Prefix"] = ws_root

        for page in paginator.paginate(**paginate_kwargs):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key == ws_root or key.endswith("/"):
                    continue
                size: int = obj["Size"]
                last_mod: datetime = obj["LastModified"]

                total_objects += 1
                total_size += size

                rel_key = key[len(ws_root) :] if (ws_root and key.startswith(ws_root)) else key
                top = rel_key.split("/")[0] if "/" in rel_key else "root"
                if top not in prefix_stats:
                    prefix_stats[top] = {"objects": 0, "size": 0, "last_modified": None}
                prefix_stats[top]["objects"] += 1
                prefix_stats[top]["size"] += size
                if (
                    prefix_stats[top]["last_modified"] is None
                    or last_mod > prefix_stats[top]["last_modified"]
                ):
                    prefix_stats[top]["last_modified"] = last_mod

                for src in SOURCES:
                    if (
                        rel_key.startswith(f"raw/{src}/")
                        or rel_key.startswith(f"{src}/")
                        or f"/raw/{src}/" in rel_key
                        or f"/{src}/" in rel_key
                    ):
                        if (
                            source_last_modified[src] is None
                            or last_mod > source_last_modified[src]
                        ):
                            source_last_modified[src] = last_mod

                # Also detect any dynamic or existing source folders under raw/
                if rel_key.startswith("raw/"):
                    parts = rel_key.split("/")
                    if len(parts) > 1 and parts[1]:
                        detected_src = parts[1]
                        if detected_src not in source_last_modified:
                            source_last_modified[detected_src] = last_mod
                        elif (
                            source_last_modified[detected_src] is None
                            or last_mod > source_last_modified[detected_src]
                        ):
                            source_last_modified[detected_src] = last_mod

        # Collect all sources (configured SOURCES plus dynamically detected sources with data)
        all_source_keys = list(SOURCES)
        for src in source_last_modified:
            if src not in all_source_keys:
                all_source_keys.append(src)

        sources = []
        for src in all_source_keys:
            lm = source_last_modified[src]
            label = SOURCE_LABELS.get(src, src.replace("_", " ").title())
            sources.append(
                {
                    "key": src,
                    "label": label,
                    "last_modified": lm,
                    "last_modified_str": lm.strftime("%Y-%m-%d %H:%M UTC") if lm else "Never",
                    "status": _source_status(lm),
                }
            )

        raw = prefix_stats.get("raw", {"objects": 0, "size": 0})
        embeddings = prefix_stats.get("embeddings", {"objects": 0, "size": 0})

        return {
            "ok": True,
            "workspace": clean_slug or None,
            "total_objects": total_objects,
            "total_size": _human_size(total_size),
            "raw_objects": raw["objects"],
            "raw_size": _human_size(raw["size"]),
            "embeddings_objects": embeddings["objects"],
            "embeddings_size": _human_size(embeddings["size"]),
            "prefix_stats": {
                k: {
                    "objects": v["objects"],
                    "size": _human_size(v["size"]),
                    "last_modified_str": (
                        v["last_modified"].strftime("%Y-%m-%d %H:%M UTC")
                        if v["last_modified"]
                        else "—"
                    ),
                }
                for k, v in prefix_stats.items()
            },
            "sources": sources,
        }
    except (
        botocore.exceptions.EndpointConnectionError,
        botocore.exceptions.ConnectionError,
        Exception,
    ) as exc:
        return {"ok": False, "error": str(exc)}


def _get_catalog_data() -> dict[str, Any]:
    try:
        s3 = _s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        groups: dict[str, dict] = {}

        for page in paginator.paginate(Bucket=MINIO_BUCKET):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                parts = key.split("/")
                group_key = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else parts[0]

                if group_key not in groups:
                    groups[group_key] = {
                        "prefix": group_key,
                        "objects": 0,
                        "size": 0,
                        "last_modified": None,
                    }
                groups[group_key]["objects"] += 1
                groups[group_key]["size"] += obj["Size"]
                lm = obj["LastModified"]
                if (
                    groups[group_key]["last_modified"] is None
                    or lm > groups[group_key]["last_modified"]
                ):
                    groups[group_key]["last_modified"] = lm

        rows = []
        for g in sorted(groups.values(), key=lambda x: x["prefix"]):
            lm = g["last_modified"]
            rows.append(
                {
                    "prefix": g["prefix"],
                    "objects": g["objects"],
                    "size": _human_size(g["size"]),
                    "last_modified": lm.strftime("%Y-%m-%d %H:%M UTC") if lm else "—",
                    "s3_path": f"s3://{MINIO_BUCKET}/{g['prefix']}/**/*.parquet",
                }
            )
        return {"ok": True, "rows": rows}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _duckdb_connection(
    load_vss: bool = False,
    register_ai: bool = True,
    max_ai_rows: int = 50,
    ai_state: dict[str, int] | None = None,
):
    config = duckdb_pool.get_s3_duckdb_config(
        endpoint=MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
    )
    con = duckdb.connect(config=config)
    duckdb_pool.load_duckdb_extensions(con, load_vss=load_vss)
    if register_ai:
        duckdb_ai.register_ai_functions(
            con,
            api_key=_get_ai_access,
            max_rows=max_ai_rows,
            state=ai_state,
            client_factory=_ai_client,
        )
    return con


duckdb_pool_instance = duckdb_pool.DuckDBConnectionPool(
    max_size=int(os.getenv("DUCKDB_POOL_SIZE", "4")),
    connection_factory=lambda: _duckdb_connection(load_vss=True, register_ai=False),
)


def _run_query(
    sql: str,
    catalogs: list[dict[str, Any]] | None = None,
    read_only: bool = False,
    max_ai_rows: int = 50,
    cancel: Any = None,
    workspace_slug: str | None = None,
    is_superadmin: bool = False,
) -> dict[str, Any]:
    if read_only:
        allowed, reason = duckdb_ai.is_read_only_sql(sql)
        if not allowed:
            classified = duckdb_pool.classify_query_error(
                Exception(reason),
                sql=sql,
                endpoint=MINIO_ENDPOINT,
                bucket=MINIO_BUCKET,
            )
            return {"ok": False, "error": reason, "error_info": classified}

    if not is_superadmin:
        allowed, reason = sql_guardrails.validate_sql_s3_paths(
            sql=sql,
            workspace_slug=workspace_slug or "default",
            catalogs=catalogs,
            is_superadmin=is_superadmin,
            bucket=MINIO_BUCKET,
        )
        if not allowed:
            return {
                "ok": False,
                "error": reason,
                "error_code": 403,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "execution_ms": 0.0,
            }

    ai_state = {"rows_processed": 0, "ai_calls": 0}
    start_ts = time.perf_counter()
    try:
        with duckdb_pool_instance.acquire(sql=sql) as con:
            if cancel is not None:
                cancel.attach(con)
                cancel.check()
            registered_views = duckdb_ai.register_catalog_views(con, catalogs)
            duckdb_ai.register_ai_functions(
                con,
                api_key=_get_ai_access,
                max_rows=max_ai_rows,
                state=ai_state,
                client_factory=_ai_client,
            )
            rel = con.execute(sql)
            columns = [desc[0] for desc in (rel.description or [])]
            rows = rel.fetchall()
            elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
            return {
                "ok": True,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "execution_ms": elapsed_ms,
                "ai_calls_made": ai_state.get("ai_calls", 0),
                "registered_views": registered_views,
            }
    except Exception as exc:
        if cancel is not None and cancel.cancelled:
            raise duckdb_ai.GenieCancelled() from exc
        classified = duckdb_pool.classify_query_error(
            exc,
            sql=sql,
            endpoint=MINIO_ENDPOINT,
            bucket=MINIO_BUCKET,
        )
        return {"ok": False, "error": str(exc), "error_info": classified}
    finally:
        if cancel is not None:
            cancel.detach()


def _ai_query(
    question: str,
    workspace_slug: str = "default",
    catalogs: list[dict[str, Any]] | None = None,
    allowed_patterns: list[str] | None = None,
) -> dict[str, Any]:
    if not _get_ai_access():
        return {"ok": False, "error": duckdb_ai.GEMINI_KEY_NOT_CONFIGURED_MSG}
    try:
        client = _ai_client()

        # The active provider decides the embedding model and dimension; only stored vectors made
        # by the same model (and of the same length) are compared.
        embed_resp = client.models.embed_content(
            contents=question, config={"task_type": "RETRIEVAL_QUERY"}
        )
        q_vec = embed_resp.embeddings[0].values

        clean_ws = _safe_identifier(workspace_slug) if workspace_slug else "default"
        active_pattern = f"s3://{MINIO_BUCKET}/workspaces/{clean_ws}/embeddings/**/*.parquet"
        patterns: list[str] = [active_pattern]

        if catalogs:
            for cat in catalogs:
                for sch in cat.get("schemas") or []:
                    for tbl in sch.get("tables") or []:
                        vec_cfg = tbl.get("vector_index") or {}
                        if isinstance(vec_cfg, dict):
                            vpath = (vec_cfg.get("s3_vector_path") or "").strip()
                            if vpath and vpath not in patterns:
                                patterns.append(vpath)
        if allowed_patterns:
            for pat in allowed_patterns:
                if pat and "embeddings" in pat and pat not in patterns:
                    patterns.append(pat)

        con = _duckdb_connection()
        try:
            valid_patterns: list[str] = []
            for pat in patterns:
                try:
                    con.execute(
                        "SELECT 1 FROM read_parquet(?, union_by_name = true) LIMIT 0", [pat]
                    )
                    valid_patterns.append(pat)
                except Exception as exc:
                    if "No files found that match the pattern" in str(exc) or "Cannot glob" in str(
                        exc
                    ):
                        continue
                    pass

            if not valid_patterns:
                rows = []
            else:
                target_pattern: Any = (
                    valid_patterns if len(valid_patterns) > 1 else valid_patterns[0]
                )
                columns = {
                    d[0]
                    for d in con.execute(
                        "SELECT * FROM read_parquet(?, union_by_name = true) LIMIT 0",
                        [target_pattern],
                    ).description
                }
                has_model = "model" in columns
                params = [q_vec, target_pattern] + ([embed_resp.model] if has_model else []) + [5]
                rows = con.execute(
                    ai_gateway_client.vector_search_sql(embed_resp.dimension, has_model), params
                ).fetchall()
        finally:
            con.close()

        context_snippets = [
            {
                "source": r[0],
                "ingested_at": str(r[1]),
                "summary": r[2],
                "score": round(float(r[3]), 4),
            }
            for r in rows
        ]
        if context_snippets:
            context_text = "\n\n".join(
                f"[{s['source']} @ {s['ingested_at']} (score={s['score']})]:\n{s['summary']}"
                for s in context_snippets
            )
            prompt = (
                "You are an analyst for a business datalakehouse called Lake of Tears. "
                "Answer the following question using the context snippets below. "
                "Be concise and cite which data sources support your answer.\n\n"
                f"Question: {question}\n\nContext:\n{context_text}"
            )
        else:
            prompt = (
                "You are an analyst for a business datalakehouse called Lake of Tears. "
                f"Note: No semantic vector embeddings were found in s3://datalake/workspaces/{clean_ws}/embeddings/**/*.parquet for this query. "
                "Answer the user's question clearly and let them know that semantic embeddings have not yet been generated "
                "for this dataset.\n\n"
                f"Question: {question}"
            )

        answer = client.models.generate_content(contents=prompt).text

        return {"ok": True, "answer": answer, "snippets": context_snippets}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

EXAMPLE_QUERIES = [
    {
        "label": "Recent Stripe charges",
        "sql": "SELECT * FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet') ORDER BY created_at DESC LIMIT 20",
    },
    {
        "label": "Top Shopify products by revenue",
        "sql": "SELECT title, sum(quantity) AS units, round(sum(price_usd * quantity), 2) AS revenue FROM read_parquet('s3://datalake/raw/shopify_line_items/**/*.parquet') GROUP BY title ORDER BY revenue DESC LIMIT 20",
    },
    {
        "label": "Open HubSpot pipeline by stage",
        "sql": "SELECT stage, count(*) AS deals, round(sum(amount_usd), 2) AS value FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet') WHERE stage NOT IN ('closedwon','closedlost') GROUP BY stage ORDER BY value DESC",
    },
    {
        "label": "Embedding count by source",
        "sql": "SELECT source, COUNT(*) AS cnt FROM read_parquet('s3://datalake/workspaces/default/embeddings/**/*.parquet') GROUP BY source ORDER BY cnt DESC",
    },
    {
        "label": "✨ AI Sentiment (ai_query)",
        "sql": "SELECT id, amount_usd, description, ai_query('gemini-3.8-flash', 'Classify payment risk in one word: ' || description) AS risk_label FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet') LIMIT 10",
    },
    {
        "label": "✨ AI Summarize (ai_summarize)",
        "sql": "SELECT source, ingested_at, ai_summarize(content) AS executive_brief FROM read_parquet('s3://datalake/workspaces/default/embeddings/**/*.parquet') LIMIT 5",
    },
    {
        "label": "✨ AI Classify (ai_classify)",
        "sql": "SELECT deal_name, stage, amount_usd, ai_classify(stage, ['early_stage', 'late_stage', 'closed']) AS funnel_bucket FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet') LIMIT 10",
    },
    {
        "label": "✨ AI Extract JSON (ai_extract)",
        "sql": "SELECT source, ai_extract(content, ['metric', 'value', 'status']) AS structured_json FROM read_parquet('s3://datalake/workspaces/default/embeddings/**/*.parquet') LIMIT 5",
    },
]

EXAMPLE_QUESTIONS = [
    "What was our revenue trend over the last 30 days?",
    "Which Shopify products are selling the most this quarter?",
    "Are there any unusual spikes in Stripe refunds?",
    "What is the current state of our sales pipeline in HubSpot?",
]


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if AUTH_ENABLED:
        token = request.cookies.get("lake_token")
        if token:
            try:
                jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
                return RedirectResponse(url="/", status_code=302)
            except jwt.InvalidTokenError:
                pass
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "registration_enabled": AUTH_REGISTRATION_ENABLED},
    )


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("lake_token")
    return resp


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    ws = getattr(request.state, "workspace", None) or {}
    slug = ws.get("slug")
    stats = _get_bucket_stats(workspace_slug=slug)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={"request": request, "stats": stats, "page": "home"},
    )


@app.get("/catalog", response_class=HTMLResponse)
async def catalog(request: Request):
    token = request.cookies.get("lake_token")
    ws = request.state.workspace
    catalogs = []
    if token and ws and isinstance(ws, dict) and ws.get("id"):
        catalogs = (
            _backend_get(
                f"/api/workspaces/{ws['id']}/catalogs",
                token,
                workspace_id=str(ws["id"]),
            )
            or []
        )
    owned_catalogs = []
    ws_id = str(ws["id"]) if ws and isinstance(ws, dict) and ws.get("id") else None
    if isinstance(catalogs, list):
        for cat in catalogs:
            if isinstance(cat, dict):
                if "schemas" in cat and isinstance(cat["schemas"], list):
                    cat["schemas"] = _sort_schemas(cat["schemas"])
                if (
                    ws_id
                    and cat.get("owner_workspace_id")
                    and str(cat.get("owner_workspace_id")) != ws_id
                    and cat.get("my_access") != "owner"
                ):
                    continue
                owned_catalogs.append(cat)
    shared_workspaces_catalogs = _resolve_ui_shared_catalogs(request)
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={
            "request": request,
            "catalogs": owned_catalogs,
            "shared_workspaces_catalogs": shared_workspaces_catalogs,
            "page": "catalog",
            "error": error,
        },
    )


def _json_safe(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, int | float | bool | str):
        return val
    if isinstance(val, list | tuple):
        return [_json_safe(x) for x in val]
    if isinstance(val, dict):
        return {k: _json_safe(v) for k, v in val.items()}
    return str(val)


@app.get("/catalog/preview")
@app.get("/api/catalog/preview")
async def api_catalog_preview(
    request: Request,
    view: str = Query(..., description="DuckDB view name (e.g. bronze.tmdb_trending)"),
    limit: int = Query(15, ge=1, le=100),
):
    token = request.cookies.get("lake_token")
    if not token and AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    clean_view = view.strip()
    if not re.match(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$", clean_view):
        return JSONResponse({"ok": False, "error": "Invalid view identifier"}, status_code=400)

    catalogs = _resolve_ui_catalogs(request)
    ws = getattr(request.state, "workspace", None)
    req_ws = str(
        request.query_params.get("workspace_slug") or request.headers.get("X-Workspace-Slug") or ""
    ).strip()
    ws_slug = (
        (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None))
        or req_ws
        or "default"
    )
    user = getattr(request.state, "user", None)
    is_superadmin = (
        user.get("role") == "superadmin"
        if isinstance(user, dict)
        else getattr(user, "role", None) == "superadmin"
    )
    sql = f"SELECT * FROM {clean_view} LIMIT {limit}"
    result = _run_query(
        sql,
        catalogs=catalogs,
        read_only=True,
        workspace_slug=ws_slug,
        is_superadmin=is_superadmin,
    )
    if result.get("ok"):
        safe_rows = [[_json_safe(c) for c in r] for r in result.get("rows", [])]
        return JSONResponse(
            {
                "ok": True,
                "columns": result.get("columns", []),
                "rows": safe_rows,
                "row_count": result.get("row_count", 0),
                "execution_ms": result.get("execution_ms", 0),
            }
        )
    return JSONResponse(
        {
            "ok": False,
            "error": result.get("error", "Failed to query preview"),
        }
    )


@app.post("/catalog/vector-sync")
async def catalog_vector_sync(request: Request):
    token = request.cookies.get("lake_token")
    if not token and AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    catalog_id = data.get("catalog_id")
    schema_id = data.get("schema_id")
    table_id = data.get("table_id")
    source_col = data.get("source_column")
    format_template = data.get("format_template")

    if not catalog_id or not schema_id or not table_id:
        return JSONResponse(
            {"ok": False, "error": "Missing catalog_id, schema_id, or table_id"},
            status_code=400,
        )

    ws = getattr(request.state, "workspace", None)
    effective_workspace_id = (
        (str(ws["id"]) if ws and isinstance(ws, dict) and "id" in ws else None)
        or request.cookies.get("lake_workspace_id")
        or request.cookies.get("lake_active_workspace")
        or data.get("workspace_id")
    )
    effective_workspace_slug = (
        (ws.get("slug") if ws and isinstance(ws, dict) else None)
        or request.cookies.get("lake_workspace_slug")
        or data.get("workspace_slug")
    )

    if source_col is not None:
        patch_payload = {
            "vector_index": {
                "enabled": True,
                "source_column": source_col,
                "format_template": format_template or "",
                "status": "syncing",
            }
        }
        _backend_patch(
            f"/api/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}",
            token or "",
            patch_payload,
            workspace_id=effective_workspace_id,
            workspace_slug=effective_workspace_slug,
        )

    sync_res, err = _backend_post_with_error(
        f"/api/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}/vector-index/sync",
        token or "",
        {},
        workspace_id=effective_workspace_id,
        workspace_slug=effective_workspace_slug,
        timeout=180.0,
    )
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    return JSONResponse({"ok": True, "table": sync_res})


@app.post("/catalog/create")
async def catalog_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    workspace_id: str = Form(""),
):
    token = request.cookies.get("lake_token")
    if not token:
        return RedirectResponse(url="/login?reason=session_expired", status_code=302)

    # Prefer the live session workspace over the form field — the form value is
    # baked in at page-render time and can be stale (cached page, back-nav, etc.).
    ws = request.state.workspace
    effective_workspace_id = (str(ws["id"]) if ws else None) or workspace_id or None
    if not effective_workspace_id:
        err = "No active workspace selected. Please select a workspace first."
        return RedirectResponse(
            url=f"/catalog?error={urllib.parse.quote_plus(err)}",
            status_code=302,
        )

    name_clean = name.strip()
    if not name_clean:
        err = "Catalog name cannot be empty."
        return RedirectResponse(
            url=f"/catalog?error={urllib.parse.quote_plus(err)}",
            status_code=302,
        )

    _, error = _backend_post_with_error(
        "/api/catalogs",
        token,
        {
            "name": name_clean,
            "description": description.strip() if description else None,
            "workspace_id": effective_workspace_id,
        },
        workspace_id=effective_workspace_id,
    )
    if error:
        return RedirectResponse(
            url=f"/catalog?error={urllib.parse.quote_plus(error)}",
            status_code=302,
        )
    return RedirectResponse(url="/catalog", status_code=302)


@app.post("/catalog/refresh-schema/{table_id}")
async def catalog_refresh_schema(request: Request, table_id: str):
    # placeholder — schema refresh is triggered via the nightly DAG or manually
    return RedirectResponse(url="/catalog", status_code=302)


@app.get("/storage", response_class=HTMLResponse)
async def storage(request: Request):
    user = getattr(request.state, "user", None) or {}
    ws = getattr(request.state, "workspace", None) or {}
    slug = ws.get("slug")
    stats = _get_bucket_stats(workspace_slug=slug)
    is_superadmin = user.get("role") in ("superadmin", "admin")
    user_role = (
        user.get("role") if is_superadmin else (ws.get("my_role") or ws.get("role") or "read")
    )
    can_delete = is_superadmin or user_role in ("admin", "write", "editor")
    all_ws = getattr(request.state, "workspaces", None) or []
    current_ws_id = str(ws.get("id")) if ws and ws.get("id") else None
    shared_workspaces = [
        w for w in all_ws if w.get("is_shared") and str(w.get("id")) != current_ws_id
    ]
    return templates.TemplateResponse(
        request=request,
        name="storage.html",
        context={
            "request": request,
            "stats": stats,
            "page": "storage",
            "minio_console_url": MINIO_CONSOLE_URL,
            "minio_bucket": MINIO_BUCKET,
            "is_superadmin": is_superadmin,
            "user_role": user_role,
            "can_delete": can_delete,
            "shared_workspaces": shared_workspaces,
        },
    )


def _resolve_ui_catalogs(request: Request) -> list[dict[str, Any]]:
    token = request.cookies.get("lake_token")
    ws = getattr(request.state, "workspace", None)
    ws_id = None
    if ws and isinstance(ws, dict) and ws.get("id"):
        ws_id = str(ws["id"])
    elif token:
        ws_cookie = request.cookies.get(_WORKSPACE_COOKIE)
        ws_header = request.headers.get("X-Workspace-Id") or request.query_params.get(
            "workspace_id"
        )
        ws_id = ws_cookie or ws_header

    catalogs: list[dict[str, Any]] = []
    if token and ws_id:
        res = _backend_get(
            f"/api/workspaces/{ws_id}/catalogs",
            token,
            workspace_id=str(ws_id),
        )
        if isinstance(res, list):
            catalogs = res
    owned_catalogs: list[dict[str, Any]] = []
    for cat in catalogs:
        if isinstance(cat, dict):
            if "schemas" in cat and isinstance(cat["schemas"], list):
                cat["schemas"] = _sort_schemas(cat["schemas"])
            if (
                ws_id
                and cat.get("owner_workspace_id")
                and str(cat.get("owner_workspace_id")) != str(ws_id)
                and cat.get("my_access") != "owner"
            ):
                continue
            owned_catalogs.append(cat)
    return owned_catalogs


def _resolve_ui_shared_catalogs(request: Request) -> list[dict[str, Any]]:
    token = request.cookies.get("lake_token")
    if not token:
        return []
    workspaces = getattr(request.state, "workspaces", None) or []
    current_ws = getattr(request.state, "workspace", None) or {}
    current_ws_id = str(current_ws.get("id")) if current_ws.get("id") else None

    groups: dict[str, dict[str, Any]] = {}

    # 1. Catalogs shared directly into current workspace via CatalogAccess
    if current_ws_id:
        res = _backend_get(
            f"/api/workspaces/{current_ws_id}/catalogs",
            token,
            workspace_id=current_ws_id,
        )
        if isinstance(res, list):
            for cat in res:
                if (
                    isinstance(cat, dict)
                    and cat.get("owner_workspace_id")
                    and str(cat.get("owner_workspace_id")) != current_ws_id
                    and cat.get("my_access") != "owner"
                ):
                    owner_id = str(cat["owner_workspace_id"])
                    owner_name = cat.get("owner_workspace_name") or "External Workspace"
                    owner_slug = cat.get("owner_workspace_slug") or owner_name.lower()
                    if owner_id not in groups:
                        groups[owner_id] = {
                            "workspace": {
                                "id": owner_id,
                                "name": owner_name,
                                "slug": owner_slug,
                                "is_shared": True,
                            },
                            "catalogs": [],
                        }
                    cat_copy = dict(cat)
                    cat_copy["is_shared"] = True
                    cat_copy["source_workspace_id"] = owner_id
                    cat_copy["source_workspace_name"] = owner_name
                    cat_copy["source_workspace_slug"] = owner_slug
                    if "schemas" in cat_copy and isinstance(cat_copy["schemas"], list):
                        cat_copy["schemas"] = _sort_schemas(cat_copy["schemas"])
                    groups[owner_id]["catalogs"].append(cat_copy)

    # 2. Catalogs from approved shared workspaces via WorkspaceShare
    shared_workspaces = [
        w for w in workspaces if w.get("is_shared") and str(w.get("id")) != current_ws_id
    ]
    for ws in shared_workspaces:
        ws_id = str(ws["id"])
        if ws_id not in groups:
            groups[ws_id] = {
                "workspace": ws,
                "catalogs": [],
            }
        res = _backend_get(
            f"/api/workspaces/{ws_id}/catalogs",
            token,
            workspace_id=ws_id,
        )
        cats = res if isinstance(res, list) else []
        for cat in cats:
            if isinstance(cat, dict):
                if any(str(c.get("id")) == str(cat.get("id")) for c in groups[ws_id]["catalogs"]):
                    continue
                cat_copy = dict(cat)
                cat_copy["is_shared"] = True
                cat_copy["source_workspace_id"] = ws["id"]
                cat_copy["source_workspace_name"] = ws["name"]
                cat_copy["source_workspace_slug"] = ws["slug"]
                if "schemas" in cat_copy and isinstance(cat_copy["schemas"], list):
                    cat_copy["schemas"] = _sort_schemas(cat_copy["schemas"])
                groups[ws_id]["catalogs"].append(cat_copy)

    return list(groups.values())


def _resolve_ui_all_catalogs(request: Request) -> list[dict[str, Any]]:
    owned = _resolve_ui_catalogs(request)
    shared_groups = _resolve_ui_shared_catalogs(request)
    combined = list(owned or [])
    seen_ids = {str(c.get("id")) for c in combined if c.get("id")}
    for group in shared_groups or []:
        for sc in group.get("catalogs") or []:
            sc_id = str(sc.get("id")) if sc.get("id") else None
            if sc_id and sc_id not in seen_ids:
                seen_ids.add(sc_id)
                combined.append(sc)
    return combined


@app.get("/sql", response_class=HTMLResponse)
async def sql_get(request: Request):
    query_sql = request.query_params.get("q") or request.query_params.get("sql")
    selected_sql = query_sql if query_sql else EXAMPLE_QUERIES[0]["sql"]
    catalogs = _resolve_ui_catalogs(request)
    shared_workspaces_catalogs = _resolve_ui_shared_catalogs(request)
    return templates.TemplateResponse(
        request=request,
        name="sql.html",
        context={
            "request": request,
            "page": "sql",
            "result": None,
            "sql": selected_sql,
            "example_queries": EXAMPLE_QUERIES,
            "catalogs": catalogs,
            "shared_workspaces_catalogs": shared_workspaces_catalogs,
            "default_datasets": duckdb_ai.get_default_datasets(MINIO_BUCKET),
            "sql_ai_functions": duckdb_ai.SQL_AI_FUNCTIONS,
        },
    )


@app.post("/sql", response_class=HTMLResponse)
async def sql_post(request: Request, sql: str = Form(...)):
    catalogs = _resolve_ui_catalogs(request)
    shared_workspaces_catalogs = _resolve_ui_shared_catalogs(request)
    all_catalogs = _resolve_ui_all_catalogs(request)
    ws = getattr(request.state, "workspace", None)
    header_ws = str(request.headers.get("X-Workspace-Slug") or "").strip()
    ws_slug = (
        (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None))
        or header_ws
        or "default"
    )
    user = getattr(request.state, "user", None)
    is_superadmin = (
        user.get("role") == "superadmin"
        if isinstance(user, dict)
        else getattr(user, "role", None) == "superadmin"
    )
    result = _run_query(
        sql,
        catalogs=all_catalogs,
        workspace_slug=ws_slug,
        is_superadmin=is_superadmin,
    )
    return templates.TemplateResponse(
        request=request,
        name="sql.html",
        context={
            "request": request,
            "page": "sql",
            "result": result,
            "sql": sql,
            "example_queries": EXAMPLE_QUERIES,
            "catalogs": catalogs,
            "shared_workspaces_catalogs": shared_workspaces_catalogs,
            "default_datasets": duckdb_ai.get_default_datasets(MINIO_BUCKET),
            "sql_ai_functions": duckdb_ai.SQL_AI_FUNCTIONS,
        },
    )


@app.get("/dashboards", response_class=HTMLResponse)
async def dashboards(request: Request):
    ws = getattr(request.state, "workspace", None)
    user = getattr(request.state, "user", None)
    user_role = user.get("role") if user else "user"
    my_role = (ws.get("my_role") if ws else None) or user_role
    return templates.TemplateResponse(
        request=request,
        name="dashboards.html",
        context={
            "request": request,
            "page": "dashboards",
            "workspace": ws,
            "user": user,
            "my_role": my_role,
            "superset_url": SUPERSET_URL,
        },
    )


@app.get("/dashboards/{dashboard_id}", response_class=HTMLResponse)
async def dashboard_view(request: Request, dashboard_id: str):
    ws = getattr(request.state, "workspace", None)
    user = getattr(request.state, "user", None)
    user_role = user.get("role") if user else "user"
    my_role = (ws.get("my_role") if ws else None) or user_role
    return templates.TemplateResponse(
        request=request,
        name="dashboard_view.html",
        context={
            "request": request,
            "page": "dashboards",
            "dashboard_id": dashboard_id,
            "workspace": ws,
            "user": user,
            "my_role": my_role,
            "superset_url": SUPERSET_URL,
        },
    )


@app.get("/pipelines", response_class=HTMLResponse)
async def pipelines(request: Request):
    ws = request.state.workspace
    user = request.state.user
    user_role = user.get("role") if user else "user"
    my_role = (ws.get("my_role") if ws else None) or user_role
    all_ws = getattr(request.state, "workspaces", None) or []
    current_ws_id = str(ws.get("id")) if ws and ws.get("id") else None
    shared_workspaces = [
        w for w in all_ws if w.get("is_shared") and str(w.get("id")) != current_ws_id
    ]
    return templates.TemplateResponse(
        request=request,
        name="pipelines.html",
        context={
            "request": request,
            "page": "pipelines",
            "workspace": ws,
            "shared_workspaces": shared_workspaces,
            "user": user,
            "my_role": my_role,
        },
    )


@app.get("/notebooks", response_class=HTMLResponse)
async def notebooks(request: Request):
    ws = request.state.workspace
    user = request.state.user
    user_role = user.get("role") if user else "user"
    my_role = (ws.get("my_role") if ws else None) or user_role
    all_ws = getattr(request.state, "workspaces", None) or []
    current_ws_id = str(ws.get("id")) if ws and ws.get("id") else None
    shared_workspaces = [
        w for w in all_ws if w.get("is_shared") and str(w.get("id")) != current_ws_id
    ]
    return templates.TemplateResponse(
        request=request,
        name="notebooks.html",
        context={
            "request": request,
            "page": "notebooks",
            "workspace": ws,
            "shared_workspaces": shared_workspaces,
            "my_role": my_role,
        },
    )


def _get_workspace_connectors(workspace_slug: str | None, stats: dict[str, Any]) -> dict[str, Any]:
    return connector_registry.resolve_workspace_feeds(workspace_slug, stats)


@app.get("/ingest", response_class=HTMLResponse)
async def ingest(request: Request):
    user = getattr(request.state, "user", None) or {}
    ws = getattr(request.state, "workspace", None) or {}
    slug = ws.get("slug")
    stats = _get_bucket_stats(workspace_slug=slug)
    catalogs = _resolve_ui_catalogs(request)
    feeds_data = _get_workspace_connectors(workspace_slug=slug, stats=stats)
    active_feeds = feeds_data["active_feeds"]
    community_connectors = feeds_data["community_connectors"]
    categories = feeds_data["categories"]
    category_counts = feeds_data.get("category_counts", {})

    all_ws = getattr(request.state, "workspaces", None) or []
    current_ws_id = str(ws.get("id")) if ws and ws.get("id") else None
    shared_workspaces = [
        w for w in all_ws if w.get("is_shared") and str(w.get("id")) != current_ws_id
    ]
    is_superadmin = user.get("role") in ("superadmin", "admin")
    my_role = user.get("role") if is_superadmin else (ws.get("my_role") or ws.get("role") or "read")

    return templates.TemplateResponse(
        request=request,
        name="ingest.html",
        context={
            "request": request,
            "stats": stats,
            "active_feeds": active_feeds,
            "community_connectors": community_connectors,
            "categories": categories,
            "category_counts": category_counts,
            "connectors": active_feeds if active_feeds else community_connectors,
            "catalogs": catalogs,
            "page": "ingest",
            "workspace": ws,
            "shared_workspaces": shared_workspaces,
            "my_role": my_role,
        },
    )


@app.post("/api/ingest/preview-file")
async def api_ingest_preview_file(
    request: Request,
    file: UploadFile = File(...),
):
    token = _get_request_token(request)
    if not token and AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    try:
        content = await file.read()
        res = ingest_service.preview_uploaded_file(content, file.filename or "data.csv")
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)
    except Exception as exc:
        logger.error("Failed to preview file: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/ingest/create-table-from-file")
async def api_ingest_create_table_from_file(
    request: Request,
    file: UploadFile = File(...),
    catalog_id: str = Form(...),
    schema_id: str = Form(...),
    schema_name: str | None = Form(None),
    table_name: str = Form(...),
    mode: str = Form("create_new"),
    description: str | None = Form(None),
):
    token = _get_request_token(request)
    if not token and AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    user = getattr(request.state, "user", None) or {}
    ws = getattr(request.state, "workspace", None) or {}
    ws_slug = (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None)) or "default"
    ws_id = str(ws.get("id")) if isinstance(ws, dict) and ws.get("id") else None

    is_superadmin = user.get("role") in ("superadmin", "admin")
    my_role = user.get("role") if is_superadmin else (ws.get("my_role") or ws.get("role") or "read")
    if my_role not in ("admin", "write", "superadmin", "owner"):
        return JSONResponse(
            {
                "ok": False,
                "error": "Write or Admin workspace permissions required to create tables",
            },
            status_code=403,
        )

    filename = file.filename or "upload"
    clean_table_name = ingest_service.slugify(table_name)
    if not clean_table_name:
        return JSONResponse({"ok": False, "error": "Invalid table name"}, status_code=400)

    try:
        content = await file.read()
        parquet_path, columns, row_count = ingest_service.convert_to_parquet_file(content, filename)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"File conversion failed: {exc}"}, status_code=400
        )

    try:
        catalogs = _resolve_ui_catalogs(request)
        target_catalog = next((c for c in catalogs if str(c.get("id")) == str(catalog_id)), None)
        if not target_catalog:
            return JSONResponse(
                {"ok": False, "error": "Target catalog not found or access denied"}, status_code=404
            )

        cat_slug = target_catalog.get("slug") or target_catalog.get("name") or "default"

        actual_schema_id = schema_id
        target_schema = None
        if schema_id == "new":
            if not schema_name or not schema_name.strip():
                return JSONResponse(
                    {"ok": False, "error": "Schema name required for new schema"}, status_code=400
                )
            clean_schema_name = ingest_service.slugify(schema_name)
            created_schema = _backend_post(
                f"/api/catalogs/{catalog_id}/schemas",
                token,
                {
                    "name": clean_schema_name,
                    "description": "Schema created via Add Data wizard",
                },
                workspace_id=ws_id,
                workspace_slug=ws_slug,
            )
            if not created_schema or not created_schema.get("id"):
                return JSONResponse(
                    {"ok": False, "error": "Failed to create new schema"}, status_code=500
                )
            actual_schema_id = str(created_schema["id"])
            target_schema = created_schema
        else:
            target_schema = next(
                (
                    s
                    for s in target_catalog.get("schemas", [])
                    if str(s.get("id")) == str(schema_id)
                ),
                None,
            )
            if not target_schema:
                return JSONResponse(
                    {"ok": False, "error": "Target schema not found"}, status_code=404
                )

        sch_slug = target_schema.get("slug") or target_schema.get("name") or "bronze"

        existing_table = next(
            (
                t
                for t in target_schema.get("tables", [])
                if t.get("slug") == clean_table_name or t.get("name") == clean_table_name
            ),
            None,
        )

        if existing_table and mode == "create_new":
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Table '{clean_table_name}' already exists in schema '{sch_slug}'. Choose a different name or select 'Append / Overwrite' mode.",
                },
                status_code=409,
            )

        s3_key, s3_path_pattern = ingest_service.build_managed_hive_path(
            workspace_slug=ws_slug,
            catalog_slug=cat_slug,
            schema_slug=sch_slug,
            table_slug=clean_table_name,
            mode=mode,
        )

        s3 = _s3_client()
        s3.upload_file(parquet_path, MINIO_BUCKET, s3_key)

        table_id = None
        if existing_table:
            table_id = str(existing_table["id"])
            _backend_patch(
                f"/api/catalogs/{catalog_id}/schemas/{actual_schema_id}/tables/{table_id}",
                token,
                {
                    "s3_path_pattern": s3_path_pattern,
                    "column_defs": columns,
                    "description": description or existing_table.get("description"),
                },
                workspace_id=ws_id,
                workspace_slug=ws_slug,
            )
        else:
            res_table = _backend_post(
                f"/api/catalogs/{catalog_id}/schemas/{actual_schema_id}/tables",
                token,
                {
                    "name": clean_table_name,
                    "description": description or f"Managed table ingested from {filename}",
                    "s3_path_pattern": s3_path_pattern,
                    "column_defs": columns,
                },
                workspace_id=ws_id,
                workspace_slug=ws_slug,
            )
            if not res_table or not res_table.get("id"):
                return JSONResponse(
                    {"ok": False, "error": "Failed to register table metadata"}, status_code=500
                )
            table_id = str(res_table["id"])

        try:
            with duckdb_pool_instance.acquire() as con:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch_slug}"')
                con.execute(
                    f'CREATE OR REPLACE VIEW "{sch_slug}"."{clean_table_name}" AS '
                    f"SELECT * FROM read_parquet('{s3_path_pattern}')"
                )
        except Exception as v_exc:
            logger.warning(
                "Could not register local DuckDB view for %s.%s: %s",
                sch_slug,
                clean_table_name,
                v_exc,
            )

        return JSONResponse(
            {
                "ok": True,
                "table_id": table_id,
                "catalog_slug": cat_slug,
                "schema_slug": sch_slug,
                "table_name": clean_table_name,
                "s3_path_pattern": s3_path_pattern,
                "columns": columns,
                "row_count": row_count,
                "sql_view": f"{sch_slug}.{clean_table_name}",
                "sql_url": f'/sql?query=SELECT * FROM "{sch_slug}"."{clean_table_name}" LIMIT 10',
                "catalog_url": f"/catalog#{cat_slug}",
            }
        )
    finally:
        if os.path.exists(parquet_path):
            try:
                os.remove(parquet_path)
            except OSError:
                pass


@app.post("/api/ingest/register-s3-path")
async def api_ingest_register_s3_path(request: Request):
    token = _get_request_token(request)
    if not token and AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    user = getattr(request.state, "user", None) or {}
    ws = getattr(request.state, "workspace", None) or {}
    ws_slug = (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None)) or "default"
    ws_id = str(ws.get("id")) if isinstance(ws, dict) and ws.get("id") else None

    is_superadmin = user.get("role") in ("superadmin", "admin")
    my_role = user.get("role") if is_superadmin else (ws.get("my_role") or ws.get("role") or "read")
    if my_role not in ("admin", "write", "superadmin", "owner"):
        return JSONResponse(
            {
                "ok": False,
                "error": "Write or Admin workspace permissions required to register tables",
            },
            status_code=403,
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    raw_path = (data.get("s3_path") or "").strip()
    catalog_id = data.get("catalog_id")
    schema_id = data.get("schema_id")
    schema_name = data.get("schema_name")
    table_name = (data.get("table_name") or "").strip()
    description = data.get("description")

    if not raw_path or not catalog_id or not schema_id or not table_name:
        return JSONResponse(
            {"ok": False, "error": "s3_path, catalog_id, schema_id, and table_name are required"},
            status_code=400,
        )

    try:
        jailed_s3 = ingest_service.validate_and_jail_s3_path(raw_path, ws_slug, bucket=MINIO_BUCKET)
    except ValueError as val_err:
        return JSONResponse({"ok": False, "error": str(val_err)}, status_code=400)

    try:
        with duckdb_pool_instance.acquire() as con:
            columns = ingest_service.inspect_s3_parquet_path(jailed_s3, con=con)
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": f"Failed to inspect Parquet schema at '{jailed_s3}': {exc}. Ensure the path contains valid Parquet files.",
            },
            status_code=400,
        )

    clean_table_name = ingest_service.slugify(table_name)
    catalogs = _resolve_ui_catalogs(request)
    target_catalog = next((c for c in catalogs if str(c.get("id")) == str(catalog_id)), None)
    if not target_catalog:
        return JSONResponse({"ok": False, "error": "Catalog not found"}, status_code=404)

    cat_slug = target_catalog.get("slug") or target_catalog.get("name") or "default"
    actual_schema_id = schema_id
    target_schema = None

    if schema_id == "new":
        if not schema_name or not schema_name.strip():
            return JSONResponse(
                {"ok": False, "error": "Schema name required for new schema"}, status_code=400
            )
        clean_schema_name = ingest_service.slugify(schema_name)
        created_schema = _backend_post(
            f"/api/catalogs/{catalog_id}/schemas",
            token,
            {"name": clean_schema_name, "description": "Created via Add Data hub"},
            workspace_id=ws_id,
            workspace_slug=ws_slug,
        )
        if not created_schema or not created_schema.get("id"):
            return JSONResponse(
                {"ok": False, "error": "Failed to create new schema"}, status_code=500
            )
        actual_schema_id = str(created_schema["id"])
        target_schema = created_schema
    else:
        target_schema = next(
            (s for s in target_catalog.get("schemas", []) if str(s.get("id")) == str(schema_id)),
            None,
        )
        if not target_schema:
            return JSONResponse({"ok": False, "error": "Target schema not found"}, status_code=404)

    sch_slug = target_schema.get("slug") or target_schema.get("name") or "bronze"

    res_table = _backend_post(
        f"/api/catalogs/{catalog_id}/schemas/{actual_schema_id}/tables",
        token,
        {
            "name": clean_table_name,
            "description": description or f"Registered from {jailed_s3}",
            "s3_path_pattern": jailed_s3,
            "column_defs": columns,
        },
        workspace_id=ws_id,
        workspace_slug=ws_slug,
    )
    if not res_table or not res_table.get("id"):
        return JSONResponse(
            {"ok": False, "error": "Failed to register table metadata"}, status_code=500
        )

    table_id = str(res_table["id"])

    try:
        with duckdb_pool_instance.acquire() as con:
            con.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch_slug}"')
            con.execute(
                f'CREATE OR REPLACE VIEW "{sch_slug}"."{clean_table_name}" AS '
                f"SELECT * FROM read_parquet('{jailed_s3}')"
            )
    except Exception as v_exc:
        logger.warning("Could not register local DuckDB view: %s", v_exc)

    return JSONResponse(
        {
            "ok": True,
            "table_id": table_id,
            "catalog_slug": cat_slug,
            "schema_slug": sch_slug,
            "table_name": clean_table_name,
            "s3_path_pattern": jailed_s3,
            "columns": columns,
            "sql_view": f"{sch_slug}.{clean_table_name}",
            "sql_url": f'/sql?query=SELECT * FROM "{sch_slug}"."{clean_table_name}" LIMIT 10',
            "catalog_url": f"/catalog#{cat_slug}",
        }
    )


@app.get("/api/ingest/connectors")
async def api_ingest_connectors(request: Request):
    token = _get_request_token(request)
    if not token and AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    ws = getattr(request.state, "workspace", None) or {}
    slug = ws.get("slug")
    stats = _get_bucket_stats(workspace_slug=slug)
    feeds_data = _get_workspace_connectors(workspace_slug=slug, stats=stats)
    return JSONResponse(
        {
            "ok": True,
            "active_feeds": feeds_data["active_feeds"],
            "community_connectors": feeds_data["community_connectors"],
            "categories": feeds_data["categories"],
            "category_counts": feeds_data.get("category_counts", {}),
            "connectors": feeds_data["community_connectors"],
        }
    )


@app.get("/ai", response_class=HTMLResponse)
async def ai_get(request: Request):
    catalogs = _resolve_ui_catalogs(request)
    ws = getattr(request.state, "workspace", None)
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "request": request,
            "page": "ai",
            "result": None,
            "question": "",
            "example_questions": EXAMPLE_QUESTIONS,
            "catalogs": catalogs,
            "default_datasets": duckdb_ai.get_default_datasets(MINIO_BUCKET),
            "sql_ai_functions": duckdb_ai.SQL_AI_FUNCTIONS,
            "workspace": ws,
        },
    )


@app.post("/ai", response_class=HTMLResponse)
async def ai_post(request: Request, question: str = Form(...)):
    ws = getattr(request.state, "workspace", None)
    ws_slug = (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None)) or "default"
    catalogs = _resolve_ui_catalogs(request)
    shared_workspaces_catalogs = _resolve_ui_shared_catalogs(request)
    all_catalogs = list(catalogs or [])
    for group in shared_workspaces_catalogs or []:
        all_catalogs.extend(group.get("catalogs") or [])
    result = _ai_query(question, workspace_slug=ws_slug, catalogs=all_catalogs)
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "request": request,
            "page": "ai",
            "result": result,
            "question": question,
            "example_questions": EXAMPLE_QUESTIONS,
            "catalogs": catalogs,
            "default_datasets": duckdb_ai.get_default_datasets(MINIO_BUCKET),
            "sql_ai_functions": duckdb_ai.SQL_AI_FUNCTIONS,
            "workspace": ws,
        },
    )


@app.get("/anomalies", response_class=HTMLResponse)
async def anomalies(request: Request):
    ws = getattr(request.state, "workspace", None)
    return templates.TemplateResponse(
        request=request,
        name="anomalies.html",
        context={"request": request, "page": "anomalies", "workspace": ws},
    )


@app.post("/anomalies/scan", response_class=HTMLResponse)
async def anomalies_scan(
    request: Request, source: str = Form(""), contamination: float = Form(0.05)
):
    token = request.cookies.get("lake_token")
    ws = getattr(request.state, "workspace", None)
    result = None
    error = None
    if ws and token:
        ws_id = str(ws.get("id"))
        payload = {"source": source.strip() or None, "contamination": contamination}
        res = _backend_post(f"/api/workspaces/{ws_id}/anomalies/detect", token, json=payload)
        if isinstance(res, dict) and res.get("ok"):
            result = res.get("summary")
        else:
            error = res.get("error") if isinstance(res, dict) else "Scan failed"
    return templates.TemplateResponse(
        request=request,
        name="anomalies.html",
        context={
            "request": request,
            "page": "anomalies",
            "workspace": ws,
            "result": result,
            "error": error,
            "source": source,
        },
    )


@app.get("/workspace/switch/{workspace_id}")
async def switch_workspace(workspace_id: str, request: Request):
    referer = request.headers.get("referer", "/")
    resp = RedirectResponse(url=referer, status_code=302)
    resp.set_cookie(_WORKSPACE_COOKIE, workspace_id, max_age=30 * 24 * 3600, samesite="lax")
    return resp


def _build_backend_headers(
    token: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id:
        headers["X-Workspace-Id"] = str(workspace_id)
    if workspace_slug:
        headers["X-Workspace-Slug"] = str(workspace_slug)
    return headers


def _backend_get(
    path: str,
    token: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> dict | list | None:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{BACKEND_URL}{path}",
                headers=_build_backend_headers(token, workspace_id, workspace_slug),
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


async def _backend_get_async(
    path: str,
    token: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{BACKEND_URL}{path}",
                headers=_build_backend_headers(token, workspace_id, workspace_slug),
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _backend_post_with_error(
    path: str,
    token: str,
    json: dict,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    timeout: float = 5.0,
) -> tuple[dict | None, str | None]:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{BACKEND_URL}{path}",
                json=json,
                headers=_build_backend_headers(token, workspace_id, workspace_slug),
            )
        if resp.status_code in (200, 201):
            return resp.json(), None

        try:
            err_data = resp.json()
            if isinstance(err_data, dict) and "detail" in err_data:
                detail = err_data["detail"]
                if isinstance(detail, list):
                    messages = [
                        item.get("msg", str(item)) if isinstance(item, dict) else str(item)
                        for item in detail
                    ]
                    return None, "; ".join(messages)
                return None, str(detail)
        except Exception:
            pass

        if resp.text.strip():
            return None, resp.text.strip()
        return None, f"Request failed with status {resp.status_code}"
    except httpx.TimeoutException:
        return None, "Backend request timed out"
    except Exception:
        return None, "Backend service unreachable"


def _backend_post(
    path: str,
    token: str,
    json: dict,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> dict | None:
    data, _ = _backend_post_with_error(path, token, json, workspace_id, workspace_slug)
    return data


def _backend_patch(
    path: str,
    token: str,
    json: dict,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> dict | None:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.patch(
                f"{BACKEND_URL}{path}",
                json=json,
                headers=_build_backend_headers(token, workspace_id, workspace_slug),
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _backend_delete(
    path: str,
    token: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> bool:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.delete(
                f"{BACKEND_URL}{path}",
                headers=_build_backend_headers(token, workspace_id, workspace_slug),
            )
        return resp.status_code in (200, 204)
    except Exception:
        pass
    return False


# ── Settings: Account ─────────────────────────────────────────────────────


@app.get("/settings/account", response_class=HTMLResponse)
async def settings_account_get(request: Request):
    token = request.cookies.get("lake_token")
    profile = _backend_get("/api/auth/me", token) if token else None
    saved = request.query_params.get("saved")
    password_saved = request.query_params.get("password_saved")
    password_error = request.query_params.get("password_error")
    return templates.TemplateResponse(
        request=request,
        name="settings/account.html",
        context={
            "request": request,
            "page": "settings_account",
            "saved": saved,
            "password_saved": password_saved,
            "password_error": password_error,
            "profile": profile,
        },
    )


@app.post("/settings/account", response_class=HTMLResponse)
async def settings_account_post(request: Request, display_name: str = Form(...)):
    token = request.cookies.get("lake_token")
    if token:
        _backend_patch("/api/auth/me", token, {"display_name": display_name})
    return RedirectResponse(url="/settings/account?saved=1", status_code=302)


@app.post("/settings/account/password", response_class=HTMLResponse)
async def settings_account_password_post(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    token = request.cookies.get("lake_token")
    if not token:
        return RedirectResponse(url="/login", status_code=302)

    if new_password != confirm_password:
        err = urllib.parse.quote_plus("New passwords do not match.")
        return RedirectResponse(url=f"/settings/account?password_error={err}", status_code=302)

    if len(new_password) < 8:
        err = urllib.parse.quote_plus("New password must be at least 8 characters long.")
        return RedirectResponse(url=f"/settings/account?password_error={err}", status_code=302)

    data, err = _backend_post_with_error(
        "/api/auth/change-password",
        token,
        {"current_password": current_password, "new_password": new_password},
    )

    if err:
        return RedirectResponse(
            url=f"/settings/account?password_error={urllib.parse.quote_plus(err)}",
            status_code=302,
        )

    resp = RedirectResponse(url="/settings/account?password_saved=1", status_code=302)
    if data and isinstance(data, dict) and data.get("token"):
        new_token = data["token"]
        try:
            payload = jwt.decode(new_token, AUTH_SECRET_KEY, algorithms=["HS256"])
            exp = payload.get("exp")
            max_age = int(exp - time.time()) if exp else None
        except Exception:
            max_age = None
        resp.set_cookie("lake_token", new_token, max_age=max_age, httponly=True, samesite="lax")
    return resp


# ── Settings: Workspace ───────────────────────────────────────────────────


@app.get("/settings/workspace", response_class=HTMLResponse)
async def settings_workspace_get(request: Request):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    members = []
    catalog_settings = {"owned": [], "shared": [], "shared_with_others": []}
    error = request.query_params.get("error")
    saved = request.query_params.get("saved")
    inbound_requests = []
    if ws and token:
        members = _backend_get(f"/api/workspaces/{ws['id']}/members", token) or []
        catalog_settings = (
            _backend_get(f"/api/workspaces/{ws['id']}/settings/catalogs", token) or catalog_settings
        )
        inbound_requests = (
            _backend_get(f"/api/workspaces/{ws['id']}/access/requests/inbound", token) or []
        )
    return templates.TemplateResponse(
        request=request,
        name="settings/workspace.html",
        context={
            "request": request,
            "page": "settings_workspace",
            "members": members,
            "workspace": ws,
            "catalog_settings": catalog_settings,
            "inbound_requests": inbound_requests,
            "error": error,
            "catalog_error": request.query_params.get("catalog_error"),
            "saved": saved,
        },
    )


@app.post("/settings/workspace/rename")
async def settings_workspace_rename(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        _backend_patch(
            f"/api/workspaces/{ws['id']}", token, {"name": name, "description": description}
        )
    return RedirectResponse(url="/settings/workspace?saved=1", status_code=302)


@app.get("/api/workspaces/{workspace_id}/candidate-members")
async def api_workspace_candidate_members(
    request: Request,
    workspace_id: str,
    q: str = "",
    limit: int = 20,
):
    token = request.cookies.get("lake_token")
    if not token:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    from urllib.parse import quote

    query_str = f"?limit={limit}"
    if q:
        query_str += f"&q={quote(q)}"
    result = await _backend_get_async(
        f"/api/workspaces/{workspace_id}/candidate-members{query_str}", token
    )
    if result is None:
        return JSONResponse([], status_code=200)
    return JSONResponse(result)


@app.post("/settings/workspace/members/add")
async def settings_workspace_add_member(
    request: Request,
    email: str = Form(...),
    role: str = Form("user"),
):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        result = _backend_post(
            f"/api/workspaces/{ws['id']}/members",
            token,
            {"email": email, "role": role},
        )
        if result is None:
            return RedirectResponse(
                url="/settings/workspace?error=member_not_found", status_code=302
            )
    return RedirectResponse(url="/settings/workspace?saved=1", status_code=302)


@app.post("/settings/workspace/members/{user_id}/remove")
async def settings_workspace_remove_member(request: Request, user_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        _backend_delete(f"/api/workspaces/{ws['id']}/members/{user_id}", token)
    return RedirectResponse(url="/settings/workspace", status_code=302)


@app.post("/settings/workspace/members/{user_id}/role")
async def settings_workspace_member_role(
    request: Request,
    user_id: str,
    role: str = Form(...),
):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        _backend_patch(f"/api/workspaces/{ws['id']}/members/{user_id}", token, {"role": role})
    return RedirectResponse(url="/settings/workspace", status_code=302)


@app.delete("/api/workspaces/{workspace_id}/shares")
async def ui_disconnect_workspace_share(workspace_id: str, request: Request):
    token = _get_request_token(request)
    if not token and AUTH_ENABLED:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    active_ws = getattr(request.state, "workspace", None) or {}
    active_ws_id = (
        str(active_ws.get("id")) if isinstance(active_ws, dict) and active_ws.get("id") else ""
    )
    if active_ws_id:
        headers["X-Workspace-Id"] = active_ws_id
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{BACKEND_URL}/api/workspaces/{workspace_id}/shares",
                headers=headers,
                cookies=request.cookies,
            )
            if resp.status_code == 204:
                return Response(status_code=204)
            return JSONResponse(resp.json() if resp.content else {}, status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)


# ── Settings: Admin ───────────────────────────────────────────────────────


@app.get("/settings/admin", response_class=HTMLResponse)
async def settings_admin_get(request: Request):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    users_list = _backend_get("/api/users", token) or []
    all_ws = _backend_get("/api/admin/workspaces", token) or []
    invitations_list = _backend_get("/api/admin/invitations", token) or []
    admin_settings = _backend_get("/api/admin/settings", token) or {
        "catalog_soft_delete_days": 30,
        "workspace_inactive_grace_period_days": 30,
        "session_inactivity_timeout_minutes": 0,
        "session_max_hours": 24,
        "gemini_api_key_configured": False,
        "gemini_api_key_masked": None,
        "gemini_default_model": duckdb_ai.DEFAULT_GEMINI_MODEL,
    }
    return templates.TemplateResponse(
        request=request,
        name="settings/admin.html",
        context={
            "request": request,
            "page": "settings_admin",
            "users": users_list,
            "workspaces": all_ws,
            "invitations": invitations_list,
            "invite_error": request.query_params.get("invite_error"),
            "admin_settings": admin_settings,
            "auth_overview": _backend_get("/api/admin/auth-overview", token),
            "ai_provider": _backend_get("/api/admin/ai-provider", token),
            "ai_error": request.query_params.get("ai_error"),
            "saved": request.query_params.get("saved"),
        },
    )


@app.post("/settings/admin/users/{user_id}/role")
async def settings_admin_user_role(
    request: Request,
    user_id: str,
    role: str = Form(...),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_patch(f"/api/users/{user_id}", token, {"role": role})
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/users/{user_id}/toggle")
async def settings_admin_user_toggle(request: Request, user_id: str):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    # fetch current state then toggle
    users_list = _backend_get("/api/users", token) or []
    target = next((u for u in users_list if str(u["id"]) == user_id), None)
    if target:
        _backend_patch(f"/api/users/{user_id}", token, {"is_active": not target["is_active"]})
    return RedirectResponse(url="/settings/admin", status_code=302)


@app.post("/settings/admin/workspaces/create")
async def settings_admin_create_workspace(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_post("/api/workspaces", token, {"name": name, "description": description})
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/catalog-settings")
async def settings_admin_catalog_settings(
    request: Request,
    catalog_soft_delete_days: int = Form(...),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_patch(
        "/api/admin/settings", token, {"catalog_soft_delete_days": catalog_soft_delete_days}
    )
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/workspace-settings")
async def settings_admin_workspace_settings(
    request: Request,
    workspace_inactive_grace_period_days: int = Form(...),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_patch(
        "/api/admin/settings",
        token,
        {"workspace_inactive_grace_period_days": workspace_inactive_grace_period_days},
    )
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/session-settings")
async def settings_admin_session_settings(
    request: Request,
    session_inactivity_timeout_minutes: int = Form(...),
    session_max_hours: int = Form(...),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_patch(
        "/api/admin/settings",
        token,
        {
            "session_inactivity_timeout_minutes": session_inactivity_timeout_minutes,
            "session_max_hours": session_max_hours,
        },
    )
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


def _backend_put(
    path: str,
    token: str,
    payload: dict[str, Any],
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
) -> tuple[int, Any]:
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.put(
                f"{BACKEND_URL}{path}",
                headers=_build_backend_headers(token, workspace_id, workspace_slug),
                json=payload,
            )
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"detail": resp.text}
    except Exception as exc:
        return 502, {"detail": f"Could not reach the backend: {exc}"}


@app.post("/settings/admin/ai-provider")
async def settings_admin_ai_provider(request: Request):
    """Save the selected AI provider's settings. Secret values are encrypted by the backend."""
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    form = await request.form()
    provider = str(form.get("provider") or "")
    if form.get("action") == "clear-secret":
        payload = {
            "provider": provider,
            "config": {str(form.get("secret_field") or ""): None},
            "activate": False,
        }
    else:
        prefix = f"cfg__{provider}__"
        config = {k[len(prefix) :]: str(v) for k, v in form.multi_items() if k.startswith(prefix)}
        payload = {"provider": provider, "config": config, "activate": True}
    status_code, data = _backend_put("/api/admin/ai-provider", token or "", payload)
    ai_gateway_client.clear_cache()
    duckdb_ai.clear_ai_cache()
    if status_code == 200:
        return RedirectResponse(url="/settings/admin?saved=1#ai-settings", status_code=302)
    detail = data.get("detail") if isinstance(data, dict) else str(data)
    message = urllib.parse.quote(str(detail or f"Backend error ({status_code})"))
    return RedirectResponse(url=f"/settings/admin?ai_error={message}#ai-settings", status_code=302)


@app.post("/settings/admin/ai-provider/test")
async def settings_admin_ai_provider_test(request: Request):
    """Try the provider with the entered (unsaved) settings and report latency and models."""
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return JSONResponse(
            {"ok": False, "message": "Superadmin access required."}, status_code=403
        )
    token = request.cookies.get("lake_token")
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = {"provider": body.get("provider") or None, "config": body.get("config") or {}}
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/admin/ai-provider/test",
                headers={"Authorization": f"Bearer {token}"} if token else {},
                json=payload,
            )
        if resp.status_code == 200:
            return JSONResponse(resp.json())
        try:
            detail = resp.json().get("detail") or resp.text
        except Exception:
            detail = resp.text
        return JSONResponse({"ok": False, "message": str(detail)}, status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Connection error: {exc}"}, status_code=502)


@app.post("/settings/admin/workspaces/{workspace_id}/mark-inactive")
async def settings_admin_workspace_mark_inactive(request: Request, workspace_id: str):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    with httpx.Client(timeout=10) as client:
        client.request(
            "DELETE",
            f"{BACKEND_URL}/api/admin/workspaces/{workspace_id}",
            json={"mode": "soft"},
            headers={"Authorization": f"Bearer {token}"},
        )
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/workspaces/{workspace_id}/restore")
async def settings_admin_workspace_restore(request: Request, workspace_id: str):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_post(f"/api/admin/workspaces/{workspace_id}/restore", token, {})
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/workspaces/{workspace_id}/delete")
async def settings_admin_workspace_delete(
    request: Request,
    workspace_id: str,
    confirm_name: str = Form(...),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    with httpx.Client(timeout=10) as client:
        client.request(
            "DELETE",
            f"{BACKEND_URL}/api/admin/workspaces/{workspace_id}",
            json={"mode": "hard", "confirm_name": confirm_name},
            headers={"Authorization": f"Bearer {token}"},
        )
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/invitations")
async def settings_admin_create_invitation(
    request: Request,
    email: str = Form(...),
    role: str = Form("user"),
    default_role: str = Form("read"),
):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")

    workspace_access = {"default": default_role}
    form_data = await request.form()
    for key, val in form_data.items():
        if key.startswith("ws_role_"):
            ws_slug = key[len("ws_role_") :]
            if val and str(val) != "none":
                workspace_access[ws_slug] = str(val)

    payload = {
        "email": email.strip(),
        "role": role.strip(),
        "workspace_access": workspace_access,
    }
    data, err = _backend_post_with_error("/api/admin/invitations", token or "", payload)
    if err:
        err_quoted = urllib.parse.quote(str(err))
        return RedirectResponse(
            url=f"/settings/admin?invite_error={err_quoted}#invitations", status_code=302
        )
    return RedirectResponse(url="/settings/admin?saved=1#invitations", status_code=302)


@app.post("/settings/admin/invitations/{invitation_id}/revoke")
async def settings_admin_revoke_invitation(request: Request, invitation_id: str):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    _backend_delete(f"/api/admin/invitations/{invitation_id}", token or "")
    return RedirectResponse(url="/settings/admin?saved=1#invitations", status_code=302)


# ── Settings: Workspace catalog management ────────────────────────────────


@app.post("/settings/workspace/catalogs/{catalog_id}/delete")
async def settings_catalog_soft_delete(request: Request, catalog_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        _backend_delete(f"/api/catalogs/{catalog_id}", token, workspace_id=str(ws["id"]))
    return RedirectResponse(url="/settings/workspace?tab=catalogs", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/reactivate")
async def settings_catalog_reactivate(request: Request, catalog_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if token and ws:
        _backend_post(
            f"/api/catalogs/{catalog_id}/reactivate", token, {}, workspace_id=str(ws["id"])
        )
    return RedirectResponse(url="/settings/workspace?tab=catalogs", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/purge")
async def settings_catalog_purge(
    request: Request,
    catalog_id: str,
    confirm_name: str = Form(...),
):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    error = None
    if token and ws:
        result = _backend_post(
            f"/api/catalogs/{catalog_id}/purge",
            token,
            {"confirm_name": confirm_name},
            workspace_id=str(ws["id"]),
        )
        if result is None:
            error = "purge_failed"
    if error:
        return RedirectResponse(
            url="/settings/workspace?tab=catalogs&error=purge_failed", status_code=302
        )
    return RedirectResponse(url="/settings/workspace?tab=catalogs", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/remove-shared")
async def settings_catalog_remove_shared(request: Request, catalog_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        _backend_delete(
            f"/api/workspaces/{ws['id']}/catalogs/{catalog_id}/shared",
            token,
            workspace_id=str(ws["id"]),
        )
    return RedirectResponse(
        url="/settings/workspace?tab=shared_with_me&saved=removed", status_code=302
    )


@app.post("/settings/workspace/catalogs/create")
async def settings_catalog_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    token = request.cookies.get("lake_token")
    if not token:
        return RedirectResponse(url="/login?reason=session_expired", status_code=302)
    ws = request.state.workspace
    base = "/settings/workspace?tab=catalogs"
    if not ws:
        err = "No active workspace selected."
        return RedirectResponse(
            url=f"{base}&catalog_error={urllib.parse.quote_plus(err)}", status_code=302
        )
    name_clean = name.strip()
    if not name_clean:
        err = "Catalog name cannot be empty."
        return RedirectResponse(
            url=f"{base}&catalog_error={urllib.parse.quote_plus(err)}", status_code=302
        )
    ws_id = str(ws["id"])
    _, error = _backend_post_with_error(
        "/api/catalogs",
        token,
        {
            "name": name_clean,
            "description": description.strip() or None,
            "workspace_id": ws_id,
        },
        workspace_id=ws_id,
    )
    if error:
        return RedirectResponse(
            url=f"{base}&catalog_error={urllib.parse.quote_plus(error)}", status_code=302
        )
    return RedirectResponse(url=f"{base}&saved=catalog_created", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/access/{access_id}/approve")
async def settings_catalog_approve_access(request: Request, catalog_id: str, access_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if token and ws:
        _backend_patch(
            f"/api/catalogs/{catalog_id}/access/{access_id}",
            token,
            {"status": "approved"},
            workspace_id=str(ws["id"]),
        )
    return RedirectResponse(
        url="/settings/workspace?tab=shared_with_others&saved=approved", status_code=302
    )


@app.post("/settings/workspace/catalogs/{catalog_id}/access/{access_id}/reject")
async def settings_catalog_reject_access(request: Request, catalog_id: str, access_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if token and ws:
        _backend_patch(
            f"/api/catalogs/{catalog_id}/access/{access_id}",
            token,
            {"status": "rejected"},
            workspace_id=str(ws["id"]),
        )
    return RedirectResponse(
        url="/settings/workspace?tab=shared_with_others&saved=rejected", status_code=302
    )


@app.post("/settings/workspace/catalogs/{catalog_id}/access/{access_id}/revoke")
async def settings_catalog_revoke_access(request: Request, catalog_id: str, access_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if token and ws:
        _backend_delete(
            f"/api/catalogs/{catalog_id}/access/{access_id}",
            token,
            workspace_id=str(ws["id"]),
        )
    return RedirectResponse(
        url="/settings/workspace?tab=shared_with_others&saved=revoked", status_code=302
    )


# ── Catalog directory & access request (browser-side fetch proxies) ───────


@app.get("/api/catalogs/directory")
async def api_catalog_directory(request: Request, workspace_id: str = ""):
    token = request.cookies.get("lake_token")
    if not token:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    result = _backend_get(f"/api/catalogs/directory?workspace_id={workspace_id}", token)
    if result is None:
        return JSONResponse({"detail": "Failed to fetch directory"}, status_code=500)
    return JSONResponse(result)


@app.post("/api/catalogs/{catalog_id}/access/request")
async def api_catalog_access_request(request: Request, catalog_id: str):
    token = request.cookies.get("lake_token")
    if not token:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/catalogs/{catalog_id}/access/request",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code in (200, 201):
            return JSONResponse({"ok": True})
        ct = resp.headers.get("content-type", "")
        detail = (
            resp.json().get("detail", "Request failed")
            if "application/json" in ct
            else "Request failed"
        )
        return JSONResponse({"ok": False, "error": detail}, status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Native Notebooks API & WebSocket Proxy ────────────────────────────────


def _get_notebook_auth_and_workspace(
    request: Request, require_write: bool = False
) -> tuple[dict[str, Any], dict[str, Any]] | JSONResponse:
    token = request.cookies.get("lake_token")
    if not token and request.headers.get("Authorization", "").startswith("Bearer "):
        token = request.headers.get("Authorization", "").split(" ", 1)[1]

    if not token:
        if not AUTH_ENABLED:
            user = {"sub": "dev@lake.local", "role": "superadmin"}
            ws = {"id": "default", "slug": "default", "name": "Default", "my_role": "admin"}
            return user, ws
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    try:
        user = jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
    except Exception:
        return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

    workspaces = _fetch_workspaces(token) or []
    req_ws = (
        request.query_params.get("workspace")
        or request.query_params.get("workspace_id")
        or request.query_params.get("workspace_slug")
        or request.headers.get("X-Workspace-Id")
        or request.headers.get("X-Workspace-Slug")
    )
    active = None
    if req_ws:
        active = next(
            (
                w
                for w in workspaces
                if str(w.get("id")) == str(req_ws) or w.get("slug") == str(req_ws)
            ),
            None,
        )
    if not active:
        active_id = request.cookies.get(_WORKSPACE_COOKIE, "")
        active = next((w for w in workspaces if str(w["id"]) == active_id), None)
    if not active and workspaces:
        active = workspaces[0]
    if not active:
        active = {
            "id": "default",
            "slug": "default",
            "name": "Default",
            "my_role": user.get("role", "member"),
        }

    user_role = user.get("role")
    ws_role = active.get("my_role") or user_role

    if require_write:
        if user_role != "superadmin" and ws_role in ("read", "viewer"):
            role_label = "viewer" if ws_role == "viewer" else "read"
            return JSONResponse(
                {
                    "detail": f"Workspace {role_label} role is read-only. Cannot modify or execute notebooks."
                },
                status_code=403,
            )

    return user, active


def _get_request_token(request: Request) -> str | None:
    tok = request.cookies.get("lake_token")
    if not tok and request.headers.get("Authorization", "").startswith("Bearer "):
        tok = request.headers.get("Authorization", "").split(" ", 1)[1]
    return tok


@app.get("/api/notebooks")
@app.get("/notebooks/api")
async def api_list_notebooks(request: Request, subpath: str = ""):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    res = await notebook_proxy.list_workspace_notebooks(ws["slug"], subpath, auth_token=token)
    return JSONResponse(res)


@app.post("/api/notebooks")
@app.post("/notebooks/api")
async def api_create_notebook(request: Request):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    item_type = body.get("type", "notebook")
    name = body.get("name")
    folder = body.get("folder") or body.get("folder_path") or ""
    if item_type == "directory":
        target = f"{folder}/{name}" if folder and name else (name or "new_folder")
        status_code, data = await notebook_proxy.create_folder(ws["slug"], target, auth_token=token)
    else:
        status_code, data = await notebook_proxy.create_notebook(
            ws["slug"], folder, name, auth_token=token
        )
    return JSONResponse(data, status_code=status_code)


@app.post("/api/notebooks/sessions")
@app.post("/notebooks/api/sessions")
async def api_create_session(request: Request):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = body.get("path", "")
    kernel_name = body.get("kernel_name", "python3")
    status_code, data = await notebook_proxy.get_or_create_session(
        ws["slug"], path, kernel_name, auth_token=token
    )
    return JSONResponse(data, status_code=status_code)


@app.post("/api/notebooks/kernels/{kernel_id}/interrupt")
@app.post("/notebooks/api/kernels/{kernel_id}/interrupt")
async def api_interrupt_kernel(request: Request, kernel_id: str):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    token = _get_request_token(request)
    status_code, data = await notebook_proxy.interrupt_kernel(kernel_id, auth_token=token)
    return JSONResponse(data, status_code=status_code)


@app.post("/api/notebooks/kernels/{kernel_id}/restart")
@app.post("/notebooks/api/kernels/{kernel_id}/restart")
async def api_restart_kernel(request: Request, kernel_id: str):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    token = _get_request_token(request)
    status_code, data = await notebook_proxy.restart_kernel(kernel_id, auth_token=token)
    return JSONResponse(data, status_code=status_code)


@app.post("/api/notebooks/run-pipeline")
@app.post("/notebooks/api/run-pipeline")
async def api_run_pipeline(request: Request):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    user, ws = auth_res
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = body.get("path", "")
    params = body.get("parameters", {})
    user_email = user.get("sub", user.get("email", "system"))
    status_code, data = await notebook_proxy.trigger_airflow_notebook_run(
        ws["slug"], path, params, user_email
    )
    if status_code >= 400 and isinstance(data, dict) and "error" not in data:
        data["error"] = data.get("detail") or data.get("title") or f"Airflow error ({status_code})"
    return JSONResponse(data, status_code=status_code)


@app.get("/api/notebooks/runs")
@app.get("/notebooks/api/runs")
async def api_notebook_runs(request: Request, path: str = ""):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    runs = notebook_proxy.list_archived_runs(ws["slug"], path)
    return JSONResponse({"runs": runs, "workspace": ws["slug"], "notebook": path})


@app.get("/api/notebooks/directories")
@app.get("/notebooks/api/directories")
async def api_list_directories(request: Request):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    folders = await notebook_proxy.list_workspace_folders(ws["slug"], auth_token=token)
    return JSONResponse({"directories": folders, "workspace": ws["slug"]})


@app.get("/api/notebooks/search")
@app.get("/notebooks/api/search")
async def api_search_notebooks(request: Request, q: str = "", subpath: str = ""):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    try:
        clean_subpath = notebook_proxy.sanitize_notebook_path(subpath) if subpath else ""
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    items = await notebook_proxy.search_workspace_items(ws["slug"], query=q, subpath=clean_subpath)
    return JSONResponse(
        {
            "items": items,
            "query": q,
            "subpath": clean_subpath,
            "workspace": ws["slug"],
        }
    )


@app.get("/workspace/manifest")
@app.get("/api/workspace/manifest")
async def api_workspace_manifest(request: Request):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    user, ws = auth_res
    token = request.cookies.get("lake_token") or ""
    ws_slug = ws.get("slug") or "default"
    ws_id = ws.get("id") or "default"

    # Parallel data fetch using asyncio.gather
    async def _fetch_catalogs():
        if not token:
            return []
        data = None
        effective_ws_id = str(ws_id) if ws_id and ws_id != "default" else None
        if effective_ws_id:
            data = await _backend_get_async(
                f"/api/workspaces/{effective_ws_id}/catalog-manifest",
                token,
            )
            if data is None:
                data = await _backend_get_async(
                    f"/api/workspaces/{effective_ws_id}/catalogs",
                    token,
                )
        if data is None and ws_slug:
            cat_query = f"?workspace_slug={urllib.parse.quote(ws_slug)}"
            data = await _backend_get_async(
                f"/api/catalogs/catalog-manifest{cat_query}",
                token,
            )
            if data is None:
                data = await _backend_get_async(
                    f"/api/catalogs/manifest{cat_query}",
                    token,
                )
            if data is None:
                data = await _backend_get_async(
                    f"/api/catalogs{cat_query}",
                    token,
                )
        return data or []

    async def _fetch_pipelines():
        if not token:
            return []
        pipe_params = f"?workspace_slug={urllib.parse.quote(ws_slug)}" if ws_slug else ""
        pipe_data = await _backend_get_async(f"/api/v1/pipelines{pipe_params}", token)
        if pipe_data and isinstance(pipe_data, dict):
            return pipe_data.get("pipelines", [])
        if isinstance(pipe_data, list):
            return pipe_data
        return []

    async def _fetch_notebooks():
        fs_items = notebook_proxy.scan_workspace_filesystem(ws_slug)
        if fs_items is not None:
            return fs_items
        return await notebook_proxy.search_workspace_items(ws_slug, query="", subpath="")

    catalogs_raw, pipelines_raw, nb_items = await asyncio.gather(
        _fetch_catalogs(),
        _fetch_pipelines(),
        _fetch_notebooks(),
        return_exceptions=True,
    )

    if isinstance(catalogs_raw, Exception):
        logger.warning("Error fetching catalogs for manifest: %s", catalogs_raw)
        catalogs_raw = []
    if isinstance(pipelines_raw, Exception):
        logger.warning("Error fetching pipelines for manifest: %s", pipelines_raw)
        pipelines_raw = []
    if isinstance(nb_items, Exception):
        logger.warning("Error fetching notebooks for manifest: %s", nb_items)
        nb_items = []

    items: list[dict[str, Any]] = []

    # 1. Process Notebooks and Folders
    for item in nb_items:
        is_dir = item.get("type") == "directory"
        parent = item.get("parent_dir", "")
        location_desc = f"/{parent}" if parent else "Workspace Root"
        if is_dir:
            items.append(
                {
                    "category": "Folders",
                    "title": item["name"],
                    "subtitle": f"Folder in {location_desc}",
                    "url": f"/notebooks?folder={urllib.parse.quote(item['path'])}",
                    "type": "folder",
                    "path": item["path"],
                }
            )
        else:
            items.append(
                {
                    "category": "Notebooks",
                    "title": item["name"],
                    "subtitle": f"Notebook in {location_desc}",
                    "url": f"/notebooks?path={urllib.parse.quote(item['path'])}",
                    "type": "notebook",
                    "path": item["path"],
                }
            )

    # 2. Process Catalogs, Schemas, Tables
    for cat in catalogs_raw:
        cat_id = cat.get("id", "")
        cat_name = cat.get("name", "")
        cat_slug = cat.get("slug", "")
        cat_desc = cat.get("description") or "Data Catalog"
        items.append(
            {
                "category": "Catalog",
                "title": cat_name,
                "subtitle": cat_desc,
                "url": f"/catalog?catalog_id={cat_id}",
                "type": "catalog",
                "catalog_id": str(cat_id),
                "path": cat_slug or cat_name,
            }
        )

        for schema in cat.get("schemas") or []:
            sch_id = schema.get("id", "")
            sch_name = schema.get("name", "")
            sch_slug = schema.get("slug", "")
            tables = schema.get("tables") or []
            tier_label = f"{schema.get('tier').capitalize()} tier • " if schema.get("tier") else ""
            table_count_label = f"{len(tables)} table{'s' if len(tables) != 1 else ''}"

            items.append(
                {
                    "category": "Schemas",
                    "title": sch_name or sch_slug,
                    "subtitle": f"Schema in {cat_name} • {tier_label}{table_count_label}",
                    "url": f"/catalog?catalog_id={cat_id}&schema_id={sch_id}",
                    "type": "schema",
                    "catalog_id": str(cat_id),
                    "schema_id": str(sch_id),
                    "path": f"{cat_slug}.{sch_slug}",
                }
            )

            for table in tables:
                tbl_id = table.get("id", "")
                tbl_name = table.get("name", "")
                tbl_slug = table.get("slug", "")
                tbl_desc = table.get("description") or ""
                sql_sch = duckdb_ai._safe_identifier(sch_slug)
                sql_tbl = duckdb_ai._safe_identifier(tbl_slug)
                cols = table.get("columns") or []
                if not cols and "column_defs" in table:
                    cols = [
                        c.get("name")
                        for c in (table.get("column_defs") or [])
                        if isinstance(c, dict) and c.get("name")
                    ]

                sub = f"Table in {cat_name}.{sql_sch} • {len(cols)} cols"
                if tbl_desc:
                    sub += f" • {tbl_desc}"

                items.append(
                    {
                        "category": "Tables",
                        "title": tbl_name or sql_tbl,
                        "subtitle": sub,
                        "url": f"/catalog?catalog_id={cat_id}&schema_id={sch_id}&table_id={tbl_id}",
                        "type": "table",
                        "catalog_id": str(cat_id),
                        "schema_id": str(sch_id),
                        "table_id": str(tbl_id),
                        "table_ident": sql_tbl,
                        "path": f"{sql_sch}.{sql_tbl}",
                        "columns": cols,
                    }
                )

    # 3. Pipelines
    if not pipelines_raw:
        pipelines_raw = [
            {
                "dag_id": "daily_ingest",
                "name": "daily_ingest",
                "desc": "Scheduled ingestion & normalization",
            },
            {"dag_id": "ecommerce_sync", "name": "ecommerce_sync", "desc": "Shopify & Stripe sync"},
        ]

    for pipe in pipelines_raw:
        dag_id = pipe.get("dag_id") or pipe.get("name", "")
        pipe_name = (
            pipe.get("display_label") or pipe.get("pipeline_name") or pipe.get("name") or dag_id
        )
        pipe_desc = pipe.get("description") or pipe.get("desc") or f"Pipeline {dag_id}"
        items.append(
            {
                "category": "Pipelines",
                "title": pipe_name,
                "subtitle": pipe_desc,
                "url": f"/pipelines?open={urllib.parse.quote(dag_id)}",
                "type": "pipeline",
                "dag_id": dag_id,
                "path": dag_id,
            }
        )

    # 4. Storage Sources
    for src in SOURCES:
        src_label = SOURCE_LABELS.get(src, src.title())
        src_desc = f"MinIO S3 prefix: raw/{src}/"
        items.append(
            {
                "category": "Storage",
                "title": f"{src_label} Storage",
                "subtitle": src_desc,
                "url": "/storage",
                "type": "storage",
                "path": f"raw/{src}",
            }
        )

    # 5. Application Navigation Pages
    nav_pages = [
        {"title": "Dashboard", "subtitle": "System overview & metrics", "url": "/"},
        {"title": "Catalog", "subtitle": "Datasets & schemas", "url": "/catalog"},
        {"title": "Storage", "subtitle": "MinIO S3 data lake buckets", "url": "/storage"},
        {"title": "SQL Editor", "subtitle": "DuckDB SQL queries", "url": "/sql"},
        {"title": "Visualizations", "subtitle": "Apache Superset dashboards", "url": "/dashboards"},
        {"title": "Pipelines", "subtitle": "Apache Airflow DAGs", "url": "/pipelines"},
        {
            "title": "Add Data",
            "subtitle": "Data ingestion launchpad & connectors",
            "url": "/ingest",
        },
        {"title": "AI Query", "subtitle": "Gemini natural language SQL", "url": "/ai"},
        {
            "title": "Anomaly Detection",
            "subtitle": "Data drift & anomaly detection",
            "url": "/anomalies",
        },
        {"title": "Notebooks", "subtitle": "Interactive Jupyter notebooks", "url": "/notebooks"},
        {
            "title": "Workspace Settings",
            "subtitle": "Manage workspace members & catalogs",
            "url": "/settings/workspace",
        },
        {
            "title": "Account Settings",
            "subtitle": "Profile & credentials",
            "url": "/settings/account",
        },
    ]
    for page in nav_pages:
        items.append(
            {
                "category": "Navigation",
                "title": page["title"],
                "subtitle": page["subtitle"],
                "url": page["url"],
                "type": "page",
                "path": page["url"],
            }
        )

    return JSONResponse(
        {
            "workspace": ws_slug,
            "version": 1,
            "count": len(items),
            "items": items,
        }
    )


@app.get("/workspace/search")
@app.get("/api/workspace/search")
async def api_workspace_search(request: Request, q: str = ""):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    user, ws = auth_res
    token = request.cookies.get("lake_token") or ""
    query = q.strip().lower()

    results: list[dict[str, Any]] = []

    # 1. Notebooks and Folders (from Jupyter workspace)
    try:
        nb_items = await notebook_proxy.search_workspace_items(ws["slug"], query=query, subpath="")
        for item in nb_items:
            is_dir = item.get("type") == "directory"
            parent = item.get("parent_dir", "")
            location_desc = f"/{parent}" if parent else "Workspace Root"
            if is_dir:
                results.append(
                    {
                        "category": "Folders",
                        "title": item["name"],
                        "subtitle": f"Folder in {location_desc}",
                        "url": f"/notebooks?folder={urllib.parse.quote(item['path'])}",
                        "type": "folder",
                        "path": item["path"],
                    }
                )
            else:
                results.append(
                    {
                        "category": "Notebooks",
                        "title": item["name"],
                        "subtitle": f"Notebook in {location_desc}",
                        "url": f"/notebooks?path={urllib.parse.quote(item['path'])}",
                        "type": "notebook",
                        "path": item["path"],
                    }
                )
    except Exception as exc:
        logger.warning("Error searching notebooks in workspace search: %s", exc)

    # 2. Catalogs, Schemas & Tables
    try:
        catalogs = []
        if token and ws and isinstance(ws, dict) and ws.get("id") and ws.get("id") != "default":
            catalogs = (
                _backend_get(
                    f"/api/workspaces/{ws['id']}/catalogs",
                    token,
                    workspace_id=str(ws["id"]),
                )
                or []
            )
        elif token and ws and isinstance(ws, dict) and ws.get("slug"):
            catalogs = (
                _backend_get(
                    "/api/catalogs",
                    token,
                    workspace_id=str(ws.get("id")) if ws.get("id") else None,
                    workspace_slug=ws.get("slug"),
                )
                or []
            )
        for cat in catalogs:
            cat_id = cat.get("id", "")
            cat_name = cat.get("name", "")
            cat_slug = cat.get("slug", "")
            cat_desc = cat.get("description") or "Data Catalog"
            if (
                not query
                or query in cat_name.lower()
                or query in cat_slug.lower()
                or query in cat_desc.lower()
            ):
                results.append(
                    {
                        "category": "Catalog",
                        "title": cat_name,
                        "subtitle": cat_desc,
                        "url": f"/catalog?catalog_id={cat_id}",
                        "type": "catalog",
                        "catalog_id": str(cat_id),
                        "path": cat_name,
                    }
                )

            for schema in cat.get("schemas") or []:
                sch_id = schema.get("id", "")
                sch_name = schema.get("name", "")
                sch_slug = schema.get("slug", "")
                sch_desc = schema.get("description") or ""
                sch_tier = (schema.get("tier") or "").lower()
                tables = schema.get("tables") or []

                schema_matches = (
                    not query
                    or query in sch_name.lower()
                    or query in sch_slug.lower()
                    or query in sch_desc.lower()
                    or query in sch_tier
                )

                if schema_matches:
                    tier_label = (
                        f"{schema.get('tier').capitalize()} tier • " if schema.get("tier") else ""
                    )
                    table_count_label = f"{len(tables)} table{'s' if len(tables) != 1 else ''}"
                    results.append(
                        {
                            "category": "Schemas",
                            "title": sch_name or sch_slug,
                            "subtitle": f"Schema in {cat_name} • {tier_label}{table_count_label}",
                            "url": f"/catalog?catalog_id={cat_id}&schema_id={sch_id}",
                            "type": "schema",
                            "catalog_id": str(cat_id),
                            "schema_id": str(sch_id),
                            "path": f"{cat_slug}.{sch_slug}",
                        }
                    )

                for table in tables:
                    tbl_id = table.get("id", "")
                    tbl_name = table.get("name", "")
                    tbl_slug = table.get("slug", "")
                    tbl_desc = table.get("description") or ""
                    tbl_s3 = table.get("s3_path_pattern") or ""
                    cols = table.get("column_defs") or []

                    direct_match = (
                        not query
                        or query in tbl_name.lower()
                        or query in tbl_slug.lower()
                        or query in tbl_desc.lower()
                        or query in tbl_s3.lower()
                    )

                    col_match = None
                    if not direct_match and query:
                        for col in cols:
                            if isinstance(col, dict) and query in str(col.get("name", "")).lower():
                                col_match = col.get("name")
                                break

                    if direct_match or col_match:
                        sql_sch = duckdb_ai._safe_identifier(sch_slug)
                        sql_tbl = duckdb_ai._safe_identifier(tbl_slug)
                        if col_match:
                            sub = f"Table in {cat_name}.{sql_sch} • Matched column: {col_match} • {len(cols)} cols"
                        elif tbl_desc:
                            sub = f"Table in {cat_name}.{sql_sch} • {len(cols)} cols • {tbl_desc}"
                        else:
                            sub = f"Table in {cat_name}.{sql_sch} • {len(cols)} cols"

                        results.append(
                            {
                                "category": "Tables",
                                "title": tbl_name or sql_tbl,
                                "subtitle": sub,
                                "url": f"/catalog?catalog_id={cat_id}&schema_id={sch_id}&table_id={tbl_id}",
                                "type": "table",
                                "catalog_id": str(cat_id),
                                "schema_id": str(sch_id),
                                "table_id": str(tbl_id),
                                "table_ident": sql_tbl,
                                "path": f"{sql_sch}.{sql_tbl}",
                            }
                        )
    except Exception as exc:
        logger.warning("Error searching catalog in workspace search: %s", exc)

    # 3. Pipelines (Airflow DAGs)
    pipelines_list = []
    try:
        if token:
            ws_id = ws.get("id") if ws else None
            ws_slug = ws.get("slug") if ws else None
            pipe_params = f"?workspace_slug={urllib.parse.quote(ws_slug)}" if ws_slug else ""
            pipe_data = _backend_get(
                f"/api/v1/pipelines{pipe_params}",
                token,
                workspace_id=ws_id,
                workspace_slug=ws_slug,
            )
            if pipe_data and isinstance(pipe_data, dict):
                pipelines_list = pipe_data.get("pipelines", [])
            elif isinstance(pipe_data, list):
                pipelines_list = pipe_data
    except Exception as exc:
        logger.warning("Error searching pipelines in workspace search: %s", exc)

    if not pipelines_list:
        pipelines_list = [
            {
                "dag_id": "daily_ingest",
                "name": "daily_ingest",
                "desc": "Scheduled ingestion & normalization",
            },
            {"dag_id": "ecommerce_sync", "name": "ecommerce_sync", "desc": "Shopify & Stripe sync"},
        ]

    for pipe in pipelines_list:
        dag_id = pipe.get("dag_id") or pipe.get("name", "")
        pipe_name = (
            pipe.get("display_label") or pipe.get("pipeline_name") or pipe.get("name") or dag_id
        )
        pipe_desc = pipe.get("description") or pipe.get("desc") or f"Pipeline {dag_id}"
        if (
            not query
            or query in pipe_name.lower()
            or query in pipe_desc.lower()
            or query in dag_id.lower()
        ):
            results.append(
                {
                    "category": "Pipelines",
                    "title": pipe_name,
                    "subtitle": pipe_desc,
                    "url": f"/pipelines?open={urllib.parse.quote(dag_id)}",
                    "type": "pipeline",
                    "dag_id": dag_id,
                    "path": dag_id,
                }
            )

    # 4. Storage Sources (S3 / MinIO)
    for src in SOURCES:
        src_label = SOURCE_LABELS.get(src, src.title())
        src_desc = f"MinIO S3 prefix: raw/{src}/"
        if not query or query in src.lower() or query in src_label.lower():
            results.append(
                {
                    "category": "Storage",
                    "title": f"{src_label} Storage",
                    "subtitle": src_desc,
                    "url": "/storage",
                    "type": "storage",
                    "path": f"raw/{src}",
                }
            )

    # 5. Application Pages
    nav_pages = [
        {"title": "Dashboard", "subtitle": "System overview & metrics", "url": "/"},
        {"title": "Catalog", "subtitle": "Datasets & schemas", "url": "/catalog"},
        {"title": "Storage", "subtitle": "MinIO S3 data lake buckets", "url": "/storage"},
        {"title": "SQL Editor", "subtitle": "DuckDB SQL queries", "url": "/sql"},
        {"title": "Visualizations", "subtitle": "Apache Superset dashboards", "url": "/dashboards"},
        {"title": "Pipelines", "subtitle": "Apache Airflow DAGs", "url": "/pipelines"},
        {
            "title": "Add Data",
            "subtitle": "Data ingestion launchpad & connectors",
            "url": "/ingest",
        },
        {"title": "AI Query", "subtitle": "Gemini natural language SQL", "url": "/ai"},
        {
            "title": "Anomaly Detection",
            "subtitle": "Data drift & anomaly detection",
            "url": "/anomalies",
        },
        {"title": "Notebooks", "subtitle": "Interactive Jupyter notebooks", "url": "/notebooks"},
        {
            "title": "Workspace Settings",
            "subtitle": "Manage workspace members & catalogs",
            "url": "/settings/workspace",
        },
        {
            "title": "Account Settings",
            "subtitle": "Profile & credentials",
            "url": "/settings/account",
        },
    ]
    for page in nav_pages:
        if not query or query in page["title"].lower() or query in page["subtitle"].lower():
            results.append(
                {
                    "category": "Navigation",
                    "title": page["title"],
                    "subtitle": page["subtitle"],
                    "url": page["url"],
                    "type": "page",
                    "path": page["url"],
                }
            )

    return JSONResponse(
        {
            "query": q,
            "workspace": ws["slug"],
            "count": len(results),
            "results": results[:60],
        }
    )


@app.get("/api/notebooks/{path:path}")
@app.get("/notebooks/api/{path:path}")
async def api_get_notebook(request: Request, path: str, download: bool = False):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=False)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    try:
        clean_path = notebook_proxy.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status_code, data = await notebook_proxy.get_notebook(ws["slug"], clean_path, auth_token=token)
    if status_code != 200:
        return JSONResponse(data, status_code=status_code)

    if download:
        filename = clean_path.split("/")[-1]
        if not filename.endswith(".ipynb"):
            filename = f"{filename}.ipynb"
        content = data.get("content", data)
        return Response(
            content=json.dumps(content, indent=2),
            media_type="application/x-ipynb+json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return JSONResponse(data, status_code=status_code)


@app.put("/api/notebooks/{path:path}")
@app.put("/notebooks/api/{path:path}")
async def api_save_notebook(request: Request, path: str):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    try:
        clean_path = notebook_proxy.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    status_code, data = await notebook_proxy.save_notebook(
        ws["slug"], clean_path, body, auth_token=token
    )
    return JSONResponse(data, status_code=status_code)


@app.delete("/api/notebooks/{path:path}")
@app.delete("/notebooks/api/{path:path}")
async def api_delete_notebook(request: Request, path: str):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    try:
        clean_path = notebook_proxy.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status_code, data = await notebook_proxy.delete_item(ws["slug"], clean_path, auth_token=token)
    return JSONResponse(data, status_code=status_code)


@app.patch("/api/notebooks/{path:path}")
@app.patch("/notebooks/api/{path:path}")
async def api_rename_notebook(request: Request, path: str):
    auth_res = _get_notebook_auth_and_workspace(request, require_write=True)
    if isinstance(auth_res, JSONResponse):
        return auth_res
    _, ws = auth_res
    token = _get_request_token(request)
    try:
        clean_path = notebook_proxy.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_path = body.get("new_path")
    if not new_path:
        return JSONResponse({"error": "new_path is required"}, status_code=400)
    try:
        clean_new_path = notebook_proxy.sanitize_notebook_path(new_path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status_code, data = await notebook_proxy.rename_item(
        ws["slug"], clean_path, clean_new_path, auth_token=token
    )
    return JSONResponse(data, status_code=status_code)


@app.websocket("/api/notebooks/kernels/{kernel_id}/channels")
@app.websocket("/notebooks/api/kernels/{kernel_id}/channels")
async def websocket_kernel_proxy(websocket: WebSocket, kernel_id: str):
    await websocket.accept()

    token = websocket.cookies.get("lake_token") or websocket.query_params.get("token")
    if not token and AUTH_ENABLED:
        await websocket.send_json(
            {
                "msg_type": "error",
                "content": {"ename": "AuthError", "evalue": "Authentication required"},
            }
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if AUTH_ENABLED and token:
        try:
            user = jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
        except Exception:
            await websocket.send_json(
                {
                    "msg_type": "error",
                    "content": {"ename": "AuthError", "evalue": "Invalid or expired token"},
                }
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        workspaces = _fetch_workspaces(token) or []
        active_id = websocket.cookies.get(_WORKSPACE_COOKIE, "")
        active = next((w for w in workspaces if str(w["id"]) == active_id), None)
        if not active and workspaces:
            active = workspaces[0]

        user_role = user.get("role")
        ws_role = (active.get("my_role") if active else None) or user_role
        if user_role != "superadmin" and ws_role in ("read", "viewer"):
            role_label = "Viewer" if ws_role == "viewer" else "Read"
            await websocket.send_json(
                {
                    "msg_type": "error",
                    "content": {
                        "ename": "PermissionError",
                        "evalue": f"{role_label} role cannot execute cells",
                    },
                }
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    # Forward to Jupyter Server WebSocket
    api_base = notebook_proxy.get_jupyter_api_base()
    ws_base = api_base.replace("http://", "ws://").replace("https://", "wss://")
    upstream_url = f"{ws_base}/api/kernels/{kernel_id}/channels"
    if notebook_proxy.JUPYTER_TOKEN:
        upstream_url += f"?token={notebook_proxy.JUPYTER_TOKEN}"

    try:
        async with websockets.connect(upstream_url, proxy=None) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await upstream.send(msg)
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as exc:
                    logger.debug("Client to upstream error: %s", exc)

            async def upstream_to_client():
                try:
                    while True:
                        msg = await upstream.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as exc:
                    logger.debug("Upstream to client error: %s", exc)

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:
        logger.warning("Upstream WebSocket connection error: %s", exc)
        try:
            await websocket.send_json(
                {
                    "msg_type": "error",
                    "content": {
                        "ename": "ConnectionError",
                        "evalue": f"Cannot connect to kernel: {exc}",
                    },
                }
            )
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:
            pass


@app.get("/health")
async def health():
    return {"status": "ok"}


# Legacy redirects
@app.get("/dashboard", response_class=RedirectResponse)
async def redirect_dashboard():
    return RedirectResponse(url="/", status_code=301)


@app.get("/browse", response_class=RedirectResponse)
async def redirect_browse():
    return RedirectResponse(url="/catalog", status_code=301)


@app.get("/query", response_class=RedirectResponse)
async def redirect_query():
    return RedirectResponse(url="/sql", status_code=301)


# ── Pipelines API Proxy ───────────────────────────────────────────────────────


@app.get("/api/pipelines")
@app.get("/api/v1/pipelines")
async def ui_list_pipelines(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]
    if request.query_params.get("workspace_slug"):
        params["workspace_slug"] = request.query_params["workspace_slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        logger.error("Failed to proxy pipelines list to backend: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/pipelines/schedule-notebook")
@app.post("/api/v1/pipelines/schedule-notebook")
async def ui_schedule_notebook(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = await request.json()
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/pipelines/schedule-notebook",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.delete("/api/pipelines/schedule-notebook/{dag_id}")
@app.delete("/api/v1/pipelines/schedule-notebook/{dag_id}")
@app.delete("/api/pipelines/{dag_id}")
@app.delete("/api/v1/pipelines/{dag_id}")
async def ui_delete_pipeline(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


ui_delete_schedule_notebook = ui_delete_pipeline


@app.get("/api/pipelines/timeline")
@app.get("/api/v1/pipelines/timeline")
async def ui_get_pipelines_timeline(
    request: Request,
    date: str | None = None,
    tz: str = "UTC",
):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {"tz": tz}
    if date:
        params["date"] = date
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]
    if request.query_params.get("workspace_slug"):
        params["workspace_slug"] = request.query_params["workspace_slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/timeline",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        logger.error("Failed to proxy pipelines timeline to backend: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/pipelines/{dag_id}/settings")
@app.get("/api/v1/pipelines/{dag_id}/settings")
async def ui_get_pipeline_settings(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/settings",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.patch("/api/pipelines/{dag_id}/settings")
@app.patch("/api/v1/pipelines/{dag_id}/settings")
async def ui_patch_pipeline_settings(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/settings",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.patch("/api/pipelines/{dag_id}/schedule")
@app.patch("/api/v1/pipelines/{dag_id}/schedule")
async def ui_patch_pipeline_schedule(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/schedule",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/pipelines/{dag_id}")
@app.get("/api/v1/pipelines/{dag_id}")
async def ui_get_pipeline(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.patch("/api/pipelines/{dag_id}")
@app.patch("/api/v1/pipelines/{dag_id}")
async def ui_patch_pipeline(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = await request.json()
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/pipelines/{dag_id}/trigger")
@app.post("/api/v1/pipelines/{dag_id}/trigger")
async def ui_trigger_pipeline(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/trigger",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/pipelines/{dag_id}/runs")
@app.get("/api/v1/pipelines/{dag_id}/runs")
async def ui_get_pipeline_runs(request: Request, dag_id: str, limit: int = 25):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {"limit": limit}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/runs",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/pipelines/{dag_id}/runs/{run_id}")
@app.get("/api/v1/pipelines/{dag_id}/runs/{run_id}")
async def ui_get_pipeline_run(request: Request, dag_id: str, run_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/runs/{run_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/pipelines/{dag_id}/runs/{run_id}/tasks")
@app.get("/api/v1/pipelines/{dag_id}/runs/{run_id}/tasks")
async def ui_get_pipeline_tasks(request: Request, dag_id: str, run_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/runs/{run_id}/tasks",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/pipelines/{dag_id}/runs/{run_id}/tasks/{task_id}/logs")
@app.get("/api/v1/pipelines/{dag_id}/runs/{run_id}/tasks/{task_id}/logs")
async def ui_get_pipeline_task_logs(
    request: Request, dag_id: str, run_id: str, task_id: str, try_number: int | None = None
):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]
    if try_number is not None:
        params["try_number"] = try_number

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/runs/{run_id}/tasks/{task_id}/logs",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/pipelines/{dag_id}/runs/{run_id}/cancel")
@app.post("/api/v1/pipelines/{dag_id}/runs/{run_id}/cancel")
async def ui_cancel_pipeline_run(request: Request, dag_id: str, run_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/pipelines/{dag_id}/runs/{run_id}/cancel",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Storage API Proxy Endpoints ───────────────────────────────────────────────


@app.get("/api/storage/stats")
@app.get("/api/v1/storage/stats")
async def ui_get_storage_stats(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/storage/stats",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/storage/browse")
@app.get("/api/v1/storage/browse")
async def ui_browse_storage(
    request: Request,
    prefix: str = "",
    limit: int = 100,
    continuation_token: str | None = None,
):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {"prefix": prefix, "limit": limit}
    if continuation_token:
        params["continuation_token"] = continuation_token
    if request.query_params.get("workspace_slug"):
        params["workspace_slug"] = request.query_params["workspace_slug"]
    elif request.query_params.get("workspace_id"):
        params["workspace_id"] = request.query_params["workspace_id"]
    elif ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/storage/browse",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/storage/objects/{key:path}/metadata")
@app.get("/api/v1/storage/objects/{key:path}/metadata")
async def ui_get_object_metadata(request: Request, key: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if request.query_params.get("workspace_slug"):
        params["workspace_slug"] = request.query_params["workspace_slug"]
    elif request.query_params.get("workspace_id"):
        params["workspace_id"] = request.query_params["workspace_id"]
    elif ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/storage/objects/{key}/metadata",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/storage/objects/{key:path}/download")
@app.get("/api/v1/storage/objects/{key:path}/download")
async def ui_get_object_download(request: Request, key: str, expires_in: int = 3600):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {"expires_in": expires_in}
    if request.query_params.get("workspace_slug"):
        params["workspace_slug"] = request.query_params["workspace_slug"]
    elif request.query_params.get("workspace_id"):
        params["workspace_id"] = request.query_params["workspace_id"]
    elif ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/storage/objects/{key}/download",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/storage/objects/{key:path}/stream")
@app.get("/api/v1/storage/objects/{key:path}/stream")
async def ui_stream_object(request: Request, key: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if request.query_params.get("workspace_slug"):
        params["workspace_slug"] = request.query_params["workspace_slug"]
    elif request.query_params.get("workspace_id"):
        params["workspace_id"] = request.query_params["workspace_id"]
    elif ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    client = httpx.AsyncClient(timeout=60.0)
    try:
        req = client.build_request(
            "GET",
            f"{BACKEND_URL}/api/v1/storage/objects/{key}/stream",
            headers=headers,
            params=params,
            cookies=request.cookies,
        )
        resp = await client.send(req, stream=True)

        async def _body_stream():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        resp_headers = {
            "Content-Type": resp.headers.get("content-type", "application/octet-stream"),
        }
        if "content-disposition" in resp.headers:
            resp_headers["Content-Disposition"] = resp.headers["content-disposition"]
        if "content-length" in resp.headers:
            resp_headers["Content-Length"] = resp.headers["content-length"]

        return StreamingResponse(
            _body_stream(),
            status_code=resp.status_code,
            headers=resp_headers,
        )
    except Exception as exc:
        await client.aclose()
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.delete("/api/storage/objects/{key:path}")
@app.delete("/api/v1/storage/objects/{key:path}")
async def ui_delete_object(request: Request, key: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                f"{BACKEND_URL}/api/v1/storage/objects/{key}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/storage/upload")
@app.post("/api/v1/storage/upload")
async def ui_upload_object(
    request: Request,
    file: UploadFile = File(...),
    prefix: str = Form(""),
):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        content = await file.read()
        files = {"file": (file.filename, content, file.content_type or "application/octet-stream")}
        data = {"prefix": prefix}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/storage/upload",
                headers=headers,
                params=params,
                files=files,
                data=data,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/storage/objects/{key:path}/upload-url")
@app.post("/api/v1/storage/objects/{key:path}/upload-url")
async def ui_get_upload_url(
    request: Request,
    key: str,
    content_type: str = Query("application/octet-stream"),
    expires_in: int = 3600,
):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {"content_type": content_type, "expires_in": expires_in}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/storage/objects/{key}/upload-url",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Dashboard API Proxy Endpoints ─────────────────────────────────────────────


@app.get("/api/dashboards/stats")
@app.get("/api/v1/dashboards/stats")
async def ui_get_dashboard_stats(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/dashboards/stats",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/dashboards/datasets")
@app.get("/api/v1/dashboards/datasets")
async def ui_get_dashboard_datasets(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/dashboards/datasets",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/dashboards")
@app.get("/api/v1/dashboards")
async def ui_list_dashboards(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/dashboards",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/dashboards")
@app.post("/api/v1/dashboards")
async def ui_create_dashboard(request: Request):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/dashboards",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/dashboards/{dashboard_id}")
@app.get("/api/v1/dashboards/{dashboard_id}")
async def ui_get_dashboard(request: Request, dashboard_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.patch("/api/dashboards/{dashboard_id}")
@app.patch("/api/v1/dashboards/{dashboard_id}")
async def ui_patch_dashboard(request: Request, dashboard_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/dashboards/{dashboard_id}/widgets")
@app.post("/api/v1/dashboards/{dashboard_id}/widgets")
async def ui_add_widget(request: Request, dashboard_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}/widgets",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.patch("/api/dashboards/{dashboard_id}/widgets/{widget_id}")
@app.patch("/api/v1/dashboards/{dashboard_id}/widgets/{widget_id}")
async def ui_update_widget(request: Request, dashboard_id: str, widget_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}/widgets/{widget_id}",
                headers=headers,
                params=params,
                json=body,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.delete("/api/dashboards/{dashboard_id}/widgets/{widget_id}")
@app.delete("/api/v1/dashboards/{dashboard_id}/widgets/{widget_id}")
async def ui_delete_widget(request: Request, dashboard_id: str, widget_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}/widgets/{widget_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/dashboards/{dashboard_id}/widgets/{widget_id}/data")
@app.get("/api/v1/dashboards/{dashboard_id}/widgets/{widget_id}/data")
async def ui_get_widget_data(
    request: Request, dashboard_id: str, widget_id: str, time_range: str | None = None
):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]
    if time_range:
        params["time_range"] = time_range

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}/widgets/{widget_id}/data",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.delete("/api/dashboards/{dashboard_id}")
@app.delete("/api/v1/dashboards/{dashboard_id}")
async def ui_delete_dashboard(request: Request, dashboard_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{BACKEND_URL}/api/v1/dashboards/{dashboard_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Native AI Query, SQL AI Assistant & Genie Conversational Agent APIs ───────


def _ui_genie_chat(
    question: str,
    mode: str = "genie",
    history: list[dict[str, Any]] | None = None,
    catalogs: list[dict[str, Any]] | None = None,
    model: str | None = None,
    cancel: Any = None,
    workspace_slug: str | None = None,
    is_superadmin: bool = False,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Execute Genie conversational data analyst loop in Lake UI."""

    def _check_cancelled() -> None:
        if cancel is not None:
            cancel.check()

    def _report(stage: str, label: str, status: str = "running", **extra: Any) -> None:
        if progress_callback is not None:
            try:
                progress_callback({"stage": stage, "label": label, "status": status, **extra})
            except Exception:
                pass

    active_key = _get_ai_access()
    if not active_key:
        return {"ok": False, "mode": mode, "error": duckdb_ai.GEMINI_KEY_NOT_CONFIGURED_MSG}

    _check_cancelled()
    if mode == "rag":
        t0 = time.time()
        _report("rag_search", "Retrieving workspace semantic embeddings...", "running")
        rag_res = _ai_query(
            question,
            workspace_slug=workspace_slug or "default",
            catalogs=catalogs,
        )
        _check_cancelled()
        if not rag_res.get("ok"):
            _report("rag_search", f"Semantic search failed: {rag_res.get('error')}", "error")
            return {"ok": False, "mode": "rag", "error": rag_res.get("error")}
        rag_ms = int((time.time() - t0) * 1000)
        _report(
            "rag_search",
            f"Retrieved {len(rag_res.get('snippets', []))} semantic snippet(s)",
            "done",
            duration_ms=rag_ms,
        )
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

    t_schema = time.time()
    _report("schema", "Inspecting workspace catalog schemas...", "running")
    schema_ctx = duckdb_ai.build_schema_prompt_context(catalogs, bucket=MINIO_BUCKET)
    schema_ms = int((time.time() - t_schema) * 1000)
    _report("schema", "Inspected workspace catalog schemas", "done", duration_ms=schema_ms)

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

    def _client_factory(api_key: str):
        return _ai_client(api_key)

    t_sql = time.time()
    _report("sql_gen", "Synthesizing DuckDB SQL query...", "running")
    try:
        raw_plan = duckdb_ai.call_gemini_text(
            synth_prompt,
            model=model,
            api_key=active_key,
            client_factory=_client_factory,
        )
        parsed_plan = duckdb_ai._parse_json_response(raw_plan)
        generated_sql = duckdb_ai._strip_code_fences(
            str(parsed_plan.get("sql") or raw_plan)
        ).strip()
        tables_used = [str(t) for t in (parsed_plan.get("tables_used") or []) if t]
        sql_ms = int((time.time() - t_sql) * 1000)
        _report(
            "sql_gen",
            f"Synthesized DuckDB SQL ({len(tables_used)} tables referenced)",
            "done",
            duration_ms=sql_ms,
            sql=generated_sql,
        )
    except Exception as exc:
        _report("sql_gen", f"Failed to synthesize SQL: {exc}", "error")
        return {"ok": False, "mode": mode, "error": f"Failed to synthesize SQL: {exc}"}

    _check_cancelled()
    t_exec = time.time()
    _report("sql_exec", "Executing query against DuckDB...", "running")
    exec_res = _run_query(
        generated_sql,
        catalogs=catalogs,
        read_only=True,
        cancel=cancel,
        workspace_slug=workspace_slug,
        is_superadmin=is_superadmin,
    )
    self_corrected = False
    initial_error: str | None = None
    exec_ms = int((time.time() - t_exec) * 1000)

    if not exec_res["ok"] and exec_res.get("error"):
        initial_error = exec_res["error"]
        _report("sql_exec", f"Query returned error: {initial_error}", "error", duration_ms=exec_ms)
        _check_cancelled()
        t_fix = time.time()
        _report("sql_fix", "Self-correcting SQL query syntax...", "running")
        fix_res = duckdb_ai.fix_sql_query(
            sql=generated_sql,
            error_message=initial_error or "",
            catalogs=catalogs,
            model=model,
            api_key=active_key,
            bucket=MINIO_BUCKET,
            client_factory=_client_factory,
        )
        if fix_res.get("ok") and fix_res.get("fixed_sql"):
            candidate_sql = fix_res["fixed_sql"]
            _check_cancelled()
            retry_res = _run_query(
                candidate_sql,
                catalogs=catalogs,
                read_only=True,
                cancel=cancel,
                workspace_slug=workspace_slug,
                is_superadmin=is_superadmin,
            )
            fix_ms = int((time.time() - t_fix) * 1000)
            if retry_res["ok"]:
                generated_sql = candidate_sql
                exec_res = retry_res
                self_corrected = True
                _report(
                    "sql_fix",
                    "Successfully auto-corrected SQL query",
                    "done",
                    duration_ms=fix_ms,
                    sql=candidate_sql,
                )
            else:
                _report(
                    "sql_fix",
                    "Self-correction candidate failed to execute",
                    "error",
                    duration_ms=fix_ms,
                )
        else:
            _report("sql_fix", "Could not generate fix for SQL error", "error")
    else:
        _report(
            "sql_exec",
            f"DuckDB query executed ({exec_res.get('row_count', 0)} row{'s' if exec_res.get('row_count', 0) != 1 else ''})",
            "done",
            duration_ms=exec_ms,
        )

    snippets: list[dict[str, Any]] = []
    _check_cancelled()
    if mode == "hybrid" or not exec_res["ok"]:
        t_rag = time.time()
        _report("rag_context", "Retrieving semantic search context...", "running")
        rag_res = _ai_query(
            question,
            workspace_slug=workspace_slug or "default",
            catalogs=catalogs,
        )
        _check_cancelled()
        if rag_res.get("ok"):
            snippets = rag_res.get("snippets", [])
        rag_ms = int((time.time() - t_rag) * 1000)
        _report(
            "rag_context",
            f"Retrieved {len(snippets)} semantic snippet(s)",
            "done",
            duration_ms=rag_ms,
        )

    columns = exec_res.get("columns", [])
    raw_rows = exec_res.get("rows", [])
    rows = [[duckdb_ai.serialize_cell(c) for c in r] for r in raw_rows]
    row_count = exec_res.get("row_count", 0)
    execution_ms = exec_res.get("execution_ms", 0.0)
    chart_config = (
        duckdb_ai.infer_chart_config(columns, rows, question=question) if exec_res["ok"] else None
    )

    if exec_res["ok"]:
        insight_prompt = (
            "You are the Lake of Tears Genie Data Analyst.\n"
            "Provide a clear, concise, data-driven answer to the user's question based on the executed DuckDB query results.\n"
            "Highlight key numbers, trends, or top items.\n\n"
            f"Question: {question}\n"
            f"Executed SQL:\n```sql\n{generated_sql}\n```\n"
            f"Columns: {columns}\n"
            f"Sample Rows ({row_count} total): {rows[:20]}\n"
        )
        if snippets:
            snippet_text = "\n".join(f"- [{s['source']}]: {s['summary']}" for s in snippets[:3])
            insight_prompt += f"\nAdditional Semantic Context:\n{snippet_text}\n"
    else:
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

    _check_cancelled()
    t_ins = time.time()
    _report("insight", "Formulating data insights...", "running")
    try:
        answer = duckdb_ai.call_gemini_text(
            insight_prompt,
            model=model,
            api_key=active_key,
            client_factory=_client_factory,
        )
        ins_ms = int((time.time() - t_ins) * 1000)
        _report("insight", "Formulated data insights", "done", duration_ms=ins_ms)
    except Exception as exc:
        answer = f"Query returned {row_count} rows. (Narrative synthesis error: {exc})"
        _report("insight", f"Narrative synthesis fallback: {exc}", "error")
    _check_cancelled()

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


@app.get("/api/ai/context")
@app.get("/api/v1/ai/context")
async def ui_ai_context(request: Request):
    catalogs = _resolve_ui_catalogs(request)
    ws = getattr(request.state, "workspace", None) or {}
    return JSONResponse(
        {
            "ok": True,
            "workspace_id": str(ws.get("id", "")),
            "workspace_slug": ws.get("slug", "default"),
            "workspace_name": ws.get("name", "Default"),
            "catalogs": catalogs,
            "default_datasets": duckdb_ai.get_default_datasets(MINIO_BUCKET),
            "sql_ai_functions": duckdb_ai.SQL_AI_FUNCTIONS,
        }
    )


@app.post("/api/ai/sql/execute")
@app.post("/api/v1/ai/sql/execute")
async def ui_ai_sql_execute(request: Request):
    body = await request.json()
    sql = str(body.get("sql") or "")
    read_only = bool(body.get("read_only", False))
    max_ai_rows = int(body.get("max_ai_rows", 50))
    catalogs = _resolve_ui_all_catalogs(request)
    ws = getattr(request.state, "workspace", None)
    req_ws = str(
        request.query_params.get("workspace_slug") or request.headers.get("X-Workspace-Slug") or ""
    ).strip()
    ws_slug = (
        (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None))
        or req_ws
        or "default"
    )
    user = getattr(request.state, "user", None)
    is_superadmin = (
        user.get("role") == "superadmin"
        if isinstance(user, dict)
        else getattr(user, "role", None) == "superadmin"
    )
    res = _run_query(
        sql,
        catalogs=catalogs,
        read_only=read_only,
        max_ai_rows=max_ai_rows,
        workspace_slug=ws_slug,
        is_superadmin=is_superadmin,
    )
    if res.get("ok"):
        res["rows"] = [[duckdb_ai.serialize_cell(c) for c in r] for r in res.get("rows", [])]
    if not res.get("ok") and res.get("error_code") == 403:
        return JSONResponse(res, status_code=403)
    return JSONResponse(res)


@app.post("/api/ai/sql/generate")
@app.post("/api/v1/ai/sql/generate")
async def ui_ai_sql_generate(request: Request):
    body = await request.json()
    prompt = str(body.get("prompt") or "")
    current_sql = body.get("current_sql")
    model = str(body["model"]) if body.get("model") else None
    catalogs = _resolve_ui_all_catalogs(request)
    res = duckdb_ai.generate_sql_from_prompt(
        prompt=prompt,
        catalogs=catalogs,
        current_sql=current_sql,
        model=model,
        api_key=_get_ai_access(),
        bucket=MINIO_BUCKET,
        client_factory=_ai_client,
    )
    return JSONResponse(res)


@app.post("/api/ai/sql/fix")
@app.post("/api/v1/ai/sql/fix")
async def ui_ai_sql_fix(request: Request):
    body = await request.json()
    sql = str(body.get("sql") or "")
    error_msg = str(body.get("error") or "")
    model = str(body["model"]) if body.get("model") else None
    catalogs = _resolve_ui_all_catalogs(request)
    res = duckdb_ai.fix_sql_query(
        sql=sql,
        error_message=error_msg,
        catalogs=catalogs,
        model=model,
        api_key=_get_ai_access(),
        bucket=MINIO_BUCKET,
        client_factory=_ai_client,
    )
    return JSONResponse(res)


@app.post("/api/ai/sql/explain")
@app.post("/api/v1/ai/sql/explain")
async def ui_ai_sql_explain(request: Request):
    body = await request.json()
    sql = str(body.get("sql") or "")
    model = str(body["model"]) if body.get("model") else None
    catalogs = _resolve_ui_all_catalogs(request)
    res = duckdb_ai.explain_sql_query(
        sql=sql,
        catalogs=catalogs,
        model=model,
        api_key=_get_ai_access(),
        bucket=MINIO_BUCKET,
        client_factory=_ai_client,
    )
    return JSONResponse(res)


_GENIE_CANCEL_TOKENS: dict[str, tuple[str, Any]] = {}


def _request_user_id(request: Request) -> str:
    """Identify the caller from the session cookie (/api/ paths bypass AuthMiddleware)."""
    token = request.cookies.get("lake_token")
    if not token:
        return ""
    try:
        payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
    except Exception:
        return ""
    return str(payload.get("sub") or payload.get("id") or payload.get("email") or "")


@app.post("/api/ai/chat")
@app.post("/api/v1/ai/chat")
async def ui_ai_chat(request: Request):
    body = await request.json()
    question = str(body.get("question") or "")
    mode = str(body.get("mode") or "genie")
    history = body.get("history") or []
    model = str(body["model"]) if body.get("model") else None
    request_id = str(body.get("request_id") or "")
    catalogs = _resolve_ui_all_catalogs(request)
    ws = getattr(request.state, "workspace", None)
    req_ws = str(
        body.get("workspace_slug")
        or request.query_params.get("workspace_slug")
        or request.headers.get("X-Workspace-Slug")
        or ""
    ).strip()
    ws_slug = (
        (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None))
        or req_ws
        or "default"
    )
    user = getattr(request.state, "user", None)
    is_superadmin = (
        user.get("role") == "superadmin"
        if isinstance(user, dict)
        else getattr(user, "role", None) == "superadmin"
    )

    cancel = duckdb_ai.CancelToken()
    if request_id:
        _GENIE_CANCEL_TOKENS[request_id] = (_request_user_id(request), cancel)

    work = asyncio.ensure_future(
        run_in_threadpool(
            _ui_genie_chat,
            question=question,
            mode=mode,
            history=history,
            catalogs=catalogs,
            model=model,
            cancel=cancel,
            workspace_slug=ws_slug,
            is_superadmin=is_superadmin,
        )
    )

    async def _watch_disconnect() -> None:
        while not work.done():
            if await request.is_disconnected():
                cancel.cancel()
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.ensure_future(_watch_disconnect())
    try:
        res = await work
    except duckdb_ai.GenieCancelled:
        res = {"ok": False, "mode": mode, "cancelled": True, "error": "Request cancelled"}
    finally:
        watcher.cancel()
        if request_id:
            _GENIE_CANCEL_TOKENS.pop(request_id, None)
    return JSONResponse(res)


@app.post("/api/ai/chat/stream")
@app.post("/api/v1/ai/chat/stream")
async def ui_ai_chat_stream(request: Request):
    body = await request.json()
    question = str(body.get("question") or "")
    mode = str(body.get("mode") or "genie")
    history = body.get("history") or []
    model = str(body["model"]) if body.get("model") else None
    request_id = str(body.get("request_id") or "")
    catalogs = _resolve_ui_all_catalogs(request)
    ws = getattr(request.state, "workspace", None)
    req_ws = str(
        body.get("workspace_slug")
        or request.query_params.get("workspace_slug")
        or request.headers.get("X-Workspace-Slug")
        or ""
    ).strip()
    ws_slug = (
        (ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None))
        or req_ws
        or "default"
    )
    user = getattr(request.state, "user", None)
    is_superadmin = (
        user.get("role") == "superadmin"
        if isinstance(user, dict)
        else getattr(user, "role", None) == "superadmin"
    )

    cancel = duckdb_ai.CancelToken()
    if request_id:
        _GENIE_CANCEL_TOKENS[request_id] = (_request_user_id(request), cancel)

    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    def on_step(evt: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ("step", evt))

    async def _worker() -> None:
        try:
            res = await run_in_threadpool(
                _ui_genie_chat,
                question=question,
                mode=mode,
                history=history,
                catalogs=catalogs,
                model=model,
                cancel=cancel,
                workspace_slug=ws_slug,
                is_superadmin=is_superadmin,
                progress_callback=on_step,
            )
            await event_queue.put(("result", res))
        except duckdb_ai.GenieCancelled:
            await event_queue.put(
                (
                    "cancelled",
                    {"ok": False, "mode": mode, "cancelled": True, "error": "Request cancelled"},
                )
            )
        except Exception as exc:
            await event_queue.put(("error", {"ok": False, "mode": mode, "error": str(exc)}))
        finally:
            await event_queue.put(None)

    worker_task = asyncio.create_task(_worker())

    async def _watch_disconnect() -> None:
        while not worker_task.done():
            if await request.is_disconnected():
                cancel.cancel()
                return
            await asyncio.sleep(0.5)

    watcher_task = asyncio.create_task(_watch_disconnect())

    async def sse_generator():
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                event_name, data = item
                yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            cancel.cancel()
            raise
        finally:
            watcher_task.cancel()
            if not worker_task.done():
                cancel.cancel()
            if request_id:
                _GENIE_CANCEL_TOKENS.pop(request_id, None)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ai/chat/cancel")
@app.post("/api/v1/ai/chat/cancel")
async def ui_ai_chat_cancel(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    request_id = str(body.get("request_id") or "")
    entry = _GENIE_CANCEL_TOKENS.get(request_id)
    if not entry or entry[0] != _request_user_id(request):
        return JSONResponse({"ok": False, "error": "No matching running request."}, status_code=404)
    entry[1].cancel()
    return JSONResponse({"ok": True})
