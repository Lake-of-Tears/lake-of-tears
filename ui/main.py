import asyncio
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
from fastapi import FastAPI, Form, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

try:
    from ui import duckdb_ai, duckdb_pool, notebook_proxy
except ImportError:
    import duckdb_ai
    import duckdb_pool
    import notebook_proxy

logger = logging.getLogger("lake.ui")

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
GEMINI_API_KEY = ""
MINIO_CONSOLE_URL = os.getenv("MINIO_CONSOLE_URL", "http://localhost:9001")
SUPERSET_URL = os.getenv("SUPERSET_URL", "http://localhost:8088")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")

SOURCES = ["stripe", "shopify", "hubspot", "postgres", "weather"]
SOURCE_LABELS = {
    "stripe": "Stripe",
    "shopify": "Shopify",
    "hubspot": "HubSpot",
    "postgres": "PostgreSQL",
    "weather": "Weather",
}

app = FastAPI(title="Lake of Tears")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Catalog slugs use hyphens but the DuckDB views use underscores; templates show the SQL name.
templates.env.filters["sql_ident"] = duckdb_ai._safe_identifier
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


_GEMINI_MODEL_CACHE: tuple[str, float] | None = None
_GEMINI_MODEL_TTL = 30.0


def _get_gemini_default_model() -> str:
    """Resolve the default Gemini model from the Admin Panel backend setting (cached briefly)."""
    global _GEMINI_MODEL_CACHE
    now = time.monotonic()
    if _GEMINI_MODEL_CACHE is not None and now - _GEMINI_MODEL_CACHE[1] < _GEMINI_MODEL_TTL:
        return _GEMINI_MODEL_CACHE[0]
    model = duckdb_ai.DEFAULT_GEMINI_MODEL
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.get(
                f"{BACKEND_URL}/api/internal/settings/gemini-key",
                headers={"X-Internal-Secret": AUTH_SECRET_KEY},
            )
        if resp.status_code == 200:
            model = str(resp.json().get("gemini_default_model") or "").strip() or model
    except Exception:
        pass
    _GEMINI_MODEL_CACHE = (model, now)
    return model


duckdb_ai.set_default_model_provider(_get_gemini_default_model)


def _get_gemini_api_key() -> str:
    """Resolve the active GEMINI_API_KEY from the Admin Panel backend setting."""
    if GEMINI_API_KEY and GEMINI_API_KEY.strip():
        return GEMINI_API_KEY.strip()
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.get(
                f"{BACKEND_URL}/api/internal/settings/gemini-key",
                headers={"X-Internal-Secret": AUTH_SECRET_KEY},
            )
        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("gemini_api_key") or "").strip()
    except Exception:
        pass
    return ""


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
        request.state.session_config = {
            "session_inactivity_timeout_minutes": 0,
            "session_max_hours": 24,
        }

        if not AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        if any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        token = request.cookies.get("lake_token")
        if not token:
            return RedirectResponse(url=f"/login?next={request.url}", status_code=302)

        try:
            payload = jwt.decode(token, AUTH_SECRET_KEY, algorithms=["HS256"])
            request.state.user = payload
        except jwt.ExpiredSignatureError:
            return RedirectResponse(url="/login?error=expired", status_code=302)
        except jwt.InvalidTokenError:
            return RedirectResponse(url="/login?error=invalid", status_code=302)

        # Inject session config for the idle-timeout JS in base.html
        import asyncio

        loop = asyncio.get_event_loop()
        session_cfg = await loop.run_in_executor(None, _get_session_config)
        workspaces = await loop.run_in_executor(None, _fetch_workspaces, token)
        if workspaces is None:
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

        return await call_next(request)


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


def _get_bucket_stats() -> dict[str, Any]:
    try:
        s3 = _s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        total_objects = total_size = 0
        prefix_stats: dict[str, dict] = {}
        source_last_modified: dict[str, datetime | None] = {s: None for s in SOURCES}

        for page in paginator.paginate(Bucket=MINIO_BUCKET):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                size: int = obj["Size"]
                last_mod: datetime = obj["LastModified"]

                total_objects += 1
                total_size += size

                top = key.split("/")[0] if "/" in key else key
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
                    if f"/{src}/" in key or key.startswith(f"{src}/"):
                        if (
                            source_last_modified[src] is None
                            or last_mod > source_last_modified[src]
                        ):
                            source_last_modified[src] = last_mod

        sources = []
        for src in SOURCES:
            lm = source_last_modified[src]
            sources.append(
                {
                    "key": src,
                    "label": SOURCE_LABELS[src],
                    "last_modified": lm,
                    "last_modified_str": lm.strftime("%Y-%m-%d %H:%M UTC") if lm else "Never",
                    "status": _source_status(lm),
                }
            )

        raw = prefix_stats.get("raw", {"objects": 0, "size": 0})
        embeddings = prefix_stats.get("embeddings", {"objects": 0, "size": 0})

        return {
            "ok": True,
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
            api_key=_get_gemini_api_key,
            max_rows=max_ai_rows,
            state=ai_state,
            client_factory=lambda api_key: genai.Client(api_key=api_key),
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

    ai_state = {"rows_processed": 0, "ai_calls": 0}
    start_ts = time.perf_counter()
    try:
        with duckdb_pool_instance.acquire() as con:
            if cancel is not None:
                cancel.attach(con)
                cancel.check()
            registered_views = duckdb_ai.register_catalog_views(con, catalogs)
            duckdb_ai.register_ai_functions(
                con,
                api_key=_get_gemini_api_key,
                max_rows=max_ai_rows,
                state=ai_state,
                client_factory=lambda api_key: genai.Client(api_key=api_key),
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


AI_VECTOR_SQL = """
    SELECT
        source,
        ingested_at,
        summary,
        array_cosine_similarity(
            embedding::DOUBLE[768],
            ?::DOUBLE[768]
        ) AS score
    FROM read_parquet(?)
    ORDER BY score DESC
    LIMIT 5
"""


def _ai_query(question: str) -> dict[str, Any]:
    active_key = _get_gemini_api_key()
    if not active_key:
        return {"ok": False, "error": duckdb_ai.GEMINI_KEY_NOT_CONFIGURED_MSG}
    try:
        client = genai.Client(api_key=active_key)

        embed_resp = client.models.embed_content(
            model="gemini-embedding-001",
            contents=question,
        )
        q_vec = embed_resp.embeddings[0].values

        s3_pattern = f"s3://{MINIO_BUCKET}/embeddings/**/*.parquet"
        con = _duckdb_connection(load_vss=True)
        rel = con.execute(AI_VECTOR_SQL, [q_vec, s3_pattern])
        rows = rel.fetchall()
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

        answer = client.models.generate_content(
            model=_get_gemini_default_model(),
            contents=prompt,
        ).text

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
        "sql": "SELECT source, COUNT(*) AS cnt FROM read_parquet('s3://datalake/embeddings/**/*.parquet') GROUP BY source ORDER BY cnt DESC",
    },
    {
        "label": "✨ AI Sentiment (ai_query)",
        "sql": "SELECT id, amount_usd, description, ai_query('gemini-3.8-flash', 'Classify payment risk in one word: ' || description) AS risk_label FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet') LIMIT 10",
    },
    {
        "label": "✨ AI Summarize (ai_summarize)",
        "sql": "SELECT source, ingested_at, ai_summarize(summary) AS executive_brief FROM read_parquet('s3://datalake/embeddings/**/*.parquet') LIMIT 5",
    },
    {
        "label": "✨ AI Classify (ai_classify)",
        "sql": "SELECT deal_name, stage, amount_usd, ai_classify(stage, ['early_stage', 'late_stage', 'closed']) AS funnel_bucket FROM read_parquet('s3://datalake/raw/hubspot_deals/**/*.parquet') LIMIT 10",
    },
    {
        "label": "✨ AI Extract JSON (ai_extract)",
        "sql": "SELECT source, ai_extract(summary, ['metric', 'value', 'status']) AS structured_json FROM read_parquet('s3://datalake/embeddings/**/*.parquet') LIMIT 5",
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
        request=request, name="login.html", context={"request": request}
    )


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("lake_token")
    return resp


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    stats = _get_bucket_stats()
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
    if token and ws:
        catalogs = _backend_get(f"/api/workspaces/{ws['id']}/catalogs", token) or []
    elif token:
        catalogs = _backend_get("/api/catalogs", token) or []
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={"request": request, "catalogs": catalogs, "page": "catalog", "error": error},
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
    sql = f"SELECT * FROM {clean_view} LIMIT {limit}"
    result = _run_query(sql, catalogs=catalogs, read_only=True)
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
    stats = _get_bucket_stats()
    user = getattr(request.state, "user", None) or {}
    ws = getattr(request.state, "workspace", None) or {}
    is_superadmin = user.get("role") == "superadmin"
    user_role = user.get("role") if is_superadmin else (ws.get("role") or "viewer")
    can_delete = is_superadmin or user_role in ("admin", "editor")
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
        },
    )


def _resolve_ui_catalogs(request: Request) -> list[dict[str, Any]]:
    token = request.cookies.get("lake_token")
    ws = getattr(request.state, "workspace", None)
    if token and ws and isinstance(ws, dict) and ws.get("id"):
        res = _backend_get(f"/api/workspaces/{ws['id']}/catalogs", token)
        if isinstance(res, list):
            return res
    if token:
        res = _backend_get("/api/catalogs", token)
        if isinstance(res, list):
            return res
    return []


@app.get("/sql", response_class=HTMLResponse)
async def sql_get(request: Request):
    query_sql = request.query_params.get("q") or request.query_params.get("sql")
    selected_sql = query_sql if query_sql else EXAMPLE_QUERIES[0]["sql"]
    catalogs = _resolve_ui_catalogs(request)
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
            "default_datasets": duckdb_ai.get_default_datasets(MINIO_BUCKET),
            "sql_ai_functions": duckdb_ai.SQL_AI_FUNCTIONS,
        },
    )


@app.post("/sql", response_class=HTMLResponse)
async def sql_post(request: Request, sql: str = Form(...)):
    catalogs = _resolve_ui_catalogs(request)
    result = _run_query(sql, catalogs=catalogs)
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
    return templates.TemplateResponse(
        request=request,
        name="pipelines.html",
        context={
            "request": request,
            "page": "pipelines",
            "workspace": ws,
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
    return templates.TemplateResponse(
        request=request,
        name="notebooks.html",
        context={
            "request": request,
            "page": "notebooks",
            "workspace": ws,
            "my_role": my_role,
        },
    )


@app.get("/ingest", response_class=HTMLResponse)
async def ingest(request: Request):
    stats = _get_bucket_stats()
    return templates.TemplateResponse(
        request=request,
        name="ingest.html",
        context={"request": request, "stats": stats, "page": "ingest"},
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
    result = _ai_query(question)
    catalogs = _resolve_ui_catalogs(request)
    ws = getattr(request.state, "workspace", None)
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
    return templates.TemplateResponse(
        request=request,
        name="anomalies.html",
        context={"request": request, "page": "anomalies"},
    )


@app.get("/workspace/switch/{workspace_id}")
async def switch_workspace(workspace_id: str, request: Request):
    referer = request.headers.get("referer", "/")
    resp = RedirectResponse(url=referer, status_code=302)
    resp.set_cookie(_WORKSPACE_COOKIE, workspace_id, max_age=30 * 24 * 3600, samesite="lax")
    return resp


def _backend_get(path: str, token: str) -> dict | list | None:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{BACKEND_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _backend_post_with_error(path: str, token: str, json: dict) -> tuple[dict | None, str | None]:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.post(
                f"{BACKEND_URL}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
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


def _backend_post(path: str, token: str, json: dict) -> dict | None:
    data, _ = _backend_post_with_error(path, token, json)
    return data


def _backend_patch(path: str, token: str, json: dict) -> dict | None:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.patch(
                f"{BACKEND_URL}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _backend_delete(path: str, token: str) -> bool:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.delete(
                f"{BACKEND_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        return resp.status_code == 200
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
    catalog_settings = {"owned": [], "shared": []}
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


# ── Settings: Admin ───────────────────────────────────────────────────────


@app.get("/settings/admin", response_class=HTMLResponse)
async def settings_admin_get(request: Request):
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    users_list = _backend_get("/api/users", token) or []
    all_ws = _backend_get("/api/admin/workspaces", token) or []
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
            "admin_settings": admin_settings,
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


@app.post("/settings/admin/ai-settings")
async def settings_admin_ai_settings(
    request: Request,
    action: str = Form("save"),
    gemini_api_key: str = Form(""),
    gemini_default_model: str | None = Form(None),
):
    global _GEMINI_MODEL_CACHE
    user = request.state.user
    if not user or user.get("role") != "superadmin":
        return RedirectResponse(url="/", status_code=302)
    token = request.cookies.get("lake_token")
    if action == "clear":
        _backend_patch("/api/admin/settings", token, {"clear_gemini_api_key": True})
        duckdb_ai.clear_ai_cache()
    elif gemini_api_key.strip():
        _backend_patch(
            "/api/admin/settings",
            token,
            {"gemini_api_key": gemini_api_key.strip()},
        )
        duckdb_ai.clear_ai_cache()
    if action != "clear" and gemini_default_model is not None:
        _backend_patch(
            "/api/admin/settings",
            token,
            {"gemini_default_model": gemini_default_model.strip()},
        )
        _GEMINI_MODEL_CACHE = None
        duckdb_ai.clear_ai_cache()
    return RedirectResponse(url="/settings/admin?saved=1", status_code=302)


@app.post("/settings/admin/ai-settings/test")
async def settings_admin_ai_settings_test(request: Request):
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
    payload = {
        "api_key": body.get("api_key"),
        "model": body.get("model") or None,
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{BACKEND_URL}/api/admin/settings/test-gemini-key",
                headers={"Authorization": f"Bearer {token}"} if token else {},
                json=payload,
            )
        if resp.status_code == 200:
            return JSONResponse(resp.json())
        return JSONResponse(
            {"ok": False, "message": f"Backend error ({resp.status_code}): {resp.text}"},
            status_code=resp.status_code,
        )
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


# ── Settings: Workspace catalog management ────────────────────────────────


@app.post("/settings/workspace/catalogs/{catalog_id}/delete")
async def settings_catalog_soft_delete(request: Request, catalog_id: str):
    ws = request.state.workspace
    token = request.cookies.get("lake_token")
    if ws and token:
        _backend_delete(f"/api/catalogs/{catalog_id}", token)
    return RedirectResponse(url="/settings/workspace?tab=catalogs", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/reactivate")
async def settings_catalog_reactivate(request: Request, catalog_id: str):
    token = request.cookies.get("lake_token")
    if token:
        _backend_post(f"/api/catalogs/{catalog_id}/reactivate", token, {})
    return RedirectResponse(url="/settings/workspace?tab=catalogs", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/purge")
async def settings_catalog_purge(
    request: Request,
    catalog_id: str,
    confirm_name: str = Form(...),
):
    token = request.cookies.get("lake_token")
    error = None
    if token:
        result = _backend_post(
            f"/api/catalogs/{catalog_id}/purge", token, {"confirm_name": confirm_name}
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
        _backend_delete(f"/api/workspaces/{ws['id']}/catalogs/{catalog_id}/shared", token)
    return RedirectResponse(url="/settings/workspace?tab=catalogs", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/access/{access_id}/approve")
async def settings_catalog_approve_access(request: Request, catalog_id: str, access_id: str):
    token = request.cookies.get("lake_token")
    if token:
        _backend_patch(
            f"/api/catalogs/{catalog_id}/access/{access_id}", token, {"status": "approved"}
        )
    return RedirectResponse(url="/settings/workspace", status_code=302)


@app.post("/settings/workspace/catalogs/{catalog_id}/access/{access_id}/reject")
async def settings_catalog_reject_access(request: Request, catalog_id: str, access_id: str):
    token = request.cookies.get("lake_token")
    if token:
        _backend_patch(
            f"/api/catalogs/{catalog_id}/access/{access_id}", token, {"status": "rejected"}
        )
    return RedirectResponse(url="/settings/workspace", status_code=302)


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
        if user_role != "superadmin" and ws_role == "viewer":
            return JSONResponse(
                {
                    "detail": "Workspace viewer role is read-only. Cannot modify or execute notebooks."
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

    # 2. Catalog Tables & Datasets
    try:
        catalogs = []
        if token and ws.get("id") != "default":
            catalogs = _backend_get(f"/api/workspaces/{ws['id']}/catalogs", token) or []
        elif token:
            catalogs = _backend_get("/api/catalogs", token) or []
        for cat in catalogs:
            cat_name = cat.get("name", "")
            cat_desc = cat.get("description") or "Data Catalog"
            if not query or query in cat_name.lower() or query in cat_desc.lower():
                results.append(
                    {
                        "category": "Catalog",
                        "title": cat_name,
                        "subtitle": cat_desc,
                        "url": "/catalog",
                        "type": "catalog",
                        "path": cat_name,
                    }
                )
    except Exception as exc:
        logger.warning("Error searching catalog in workspace search: %s", exc)

    # 3. Pipelines (Airflow DAGs)
    pipelines_list = []
    try:
        if token:
            ws_slug = ws.get("slug") if ws else None
            pipe_params = f"?workspace_slug={urllib.parse.quote(ws_slug)}" if ws_slug else ""
            pipe_data = _backend_get(f"/api/v1/pipelines{pipe_params}", token)
            if pipe_data and isinstance(pipe_data, dict):
                pipelines_list = pipe_data.get("pipelines", [])
            elif isinstance(pipe_data, list):
                pipelines_list = pipe_data
    except Exception as exc:
        logger.warning("Error searching pipelines in workspace search: %s", exc)

    if not pipelines_list:
        pipelines_list = [
            {
                "dag_id": "run_notebook",
                "name": "run_notebook",
                "desc": "On-demand execution of notebooks via Airflow",
            },
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
        {"title": "Data Ingestion", "subtitle": "Connectors & ingestion status", "url": "/ingest"},
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
async def api_get_notebook(request: Request, path: str):
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
        if user_role != "superadmin" and ws_role == "viewer":
            await websocket.send_json(
                {
                    "msg_type": "error",
                    "content": {
                        "ename": "PermissionError",
                        "evalue": "Viewer role cannot execute cells",
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
        async with websockets.connect(upstream_url) as upstream:

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
async def ui_delete_schedule_notebook(request: Request, dag_id: str):
    token = _get_request_token(request)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = getattr(request.state, "workspace", None)
    params = {}
    if ws and ws.get("slug"):
        params["workspace_slug"] = ws["slug"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{BACKEND_URL}/api/v1/pipelines/schedule-notebook/{dag_id}",
                headers=headers,
                params=params,
                cookies=request.cookies,
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


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
    if ws and ws.get("slug"):
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
    if ws and ws.get("slug"):
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
    if ws and ws.get("slug"):
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
    if ws and ws.get("slug"):
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
) -> dict[str, Any]:
    """Execute Genie conversational data analyst loop in Lake UI."""

    def _check_cancelled() -> None:
        if cancel is not None:
            cancel.check()

    active_key = _get_gemini_api_key()
    if not active_key:
        return {"ok": False, "mode": mode, "error": duckdb_ai.GEMINI_KEY_NOT_CONFIGURED_MSG}

    _check_cancelled()
    if mode == "rag":
        rag_res = _ai_query(question)
        _check_cancelled()
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

    schema_ctx = duckdb_ai.build_schema_prompt_context(catalogs, bucket=MINIO_BUCKET)
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
        return genai.Client(api_key=api_key)

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
    except Exception as exc:
        return {"ok": False, "mode": mode, "error": f"Failed to synthesize SQL: {exc}"}

    _check_cancelled()
    exec_res = _run_query(generated_sql, catalogs=catalogs, read_only=True, cancel=cancel)
    self_corrected = False
    initial_error: str | None = None

    if not exec_res["ok"] and exec_res.get("error"):
        initial_error = exec_res["error"]
        _check_cancelled()
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
            retry_res = _run_query(candidate_sql, catalogs=catalogs, read_only=True, cancel=cancel)
            if retry_res["ok"]:
                generated_sql = candidate_sql
                exec_res = retry_res
                self_corrected = True

    snippets: list[dict[str, Any]] = []
    _check_cancelled()
    if mode == "hybrid" or not exec_res["ok"]:
        rag_res = _ai_query(question)
        _check_cancelled()
        if rag_res.get("ok"):
            snippets = rag_res.get("snippets", [])

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
    try:
        answer = duckdb_ai.call_gemini_text(
            insight_prompt,
            model=model,
            api_key=active_key,
            client_factory=_client_factory,
        )
    except Exception as exc:
        answer = f"Query returned {row_count} rows. (Narrative synthesis error: {exc})"
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
    catalogs = _resolve_ui_catalogs(request)
    res = _run_query(sql, catalogs=catalogs, read_only=read_only, max_ai_rows=max_ai_rows)
    if res.get("ok"):
        res["rows"] = [[duckdb_ai.serialize_cell(c) for c in r] for r in res.get("rows", [])]
    return JSONResponse(res)


@app.post("/api/ai/sql/generate")
@app.post("/api/v1/ai/sql/generate")
async def ui_ai_sql_generate(request: Request):
    body = await request.json()
    prompt = str(body.get("prompt") or "")
    current_sql = body.get("current_sql")
    model = str(body["model"]) if body.get("model") else None
    catalogs = _resolve_ui_catalogs(request)
    res = duckdb_ai.generate_sql_from_prompt(
        prompt=prompt,
        catalogs=catalogs,
        current_sql=current_sql,
        model=model,
        api_key=_get_gemini_api_key(),
        bucket=MINIO_BUCKET,
        client_factory=lambda api_key: genai.Client(api_key=api_key),
    )
    return JSONResponse(res)


@app.post("/api/ai/sql/fix")
@app.post("/api/v1/ai/sql/fix")
async def ui_ai_sql_fix(request: Request):
    body = await request.json()
    sql = str(body.get("sql") or "")
    error_msg = str(body.get("error") or "")
    model = str(body["model"]) if body.get("model") else None
    catalogs = _resolve_ui_catalogs(request)
    res = duckdb_ai.fix_sql_query(
        sql=sql,
        error_message=error_msg,
        catalogs=catalogs,
        model=model,
        api_key=_get_gemini_api_key(),
        bucket=MINIO_BUCKET,
        client_factory=lambda api_key: genai.Client(api_key=api_key),
    )
    return JSONResponse(res)


@app.post("/api/ai/sql/explain")
@app.post("/api/v1/ai/sql/explain")
async def ui_ai_sql_explain(request: Request):
    body = await request.json()
    sql = str(body.get("sql") or "")
    model = str(body["model"]) if body.get("model") else None
    catalogs = _resolve_ui_catalogs(request)
    res = duckdb_ai.explain_sql_query(
        sql=sql,
        catalogs=catalogs,
        model=model,
        api_key=_get_gemini_api_key(),
        bucket=MINIO_BUCKET,
        client_factory=lambda api_key: genai.Client(api_key=api_key),
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
    catalogs = _resolve_ui_catalogs(request)

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
