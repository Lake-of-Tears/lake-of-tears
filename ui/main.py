from __future__ import annotations

import math
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions
import duckdb
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from starlette.middleware.base import BaseHTTPMiddleware

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
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
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_PUBLIC_PREFIXES = ("/login", "/logout", "/api/", "/health")
_WORKSPACE_COOKIE = "lake_workspace_id"

_SESSION_CONFIG_CACHE: dict | None = None
_SESSION_CONFIG_FETCHED_AT: float = 0.0
_SESSION_CONFIG_TTL = 60.0  # seconds


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


def _fetch_workspaces(token: str) -> list[dict]:
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.get(
                f"{BACKEND_URL}/api/workspaces",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            return resp.json()
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
    return boto3.client(
        "s3",
        endpoint_url=f"http://{MINIO_ENDPOINT}",
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
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


def _duckdb_connection():
    con = duckdb.connect()
    con.execute(f"""
        INSTALL httpfs; LOAD httpfs;
        SET s3_endpoint='{MINIO_ENDPOINT}';
        SET s3_access_key_id='{MINIO_ACCESS_KEY}';
        SET s3_secret_access_key='{MINIO_SECRET_KEY}';
        SET s3_use_ssl=false;
        SET s3_url_style='path';
    """)
    return con


def _run_query(sql: str) -> dict[str, Any]:
    try:
        con = _duckdb_connection()
        rel = con.execute(sql)
        columns = [desc[0] for desc in rel.description]
        rows = rel.fetchall()
        con.close()
        return {"ok": True, "columns": columns, "rows": rows, "row_count": len(rows)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _ai_query(question: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY is not set."}
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        embed_resp = client.models.embed_content(
            model="gemini-embedding-001",
            contents=question,
        )
        q_vec = embed_resp.embeddings[0].values
        dim = len(q_vec)
        vec_literal = "[" + ", ".join(str(v) for v in q_vec) + "]"

        sql = f"""
            INSTALL httpfs; LOAD httpfs; INSTALL vss; LOAD vss;
            SET s3_endpoint='{MINIO_ENDPOINT}';
            SET s3_access_key_id='{MINIO_ACCESS_KEY}';
            SET s3_secret_access_key='{MINIO_SECRET_KEY}';
            SET s3_use_ssl=false;
            SET s3_url_style='path';
            SELECT
                source,
                ingested_at,
                summary,
                array_cosine_similarity(
                    embedding::DOUBLE[{dim}],
                    {vec_literal}::DOUBLE[{dim}]
                ) AS score
            FROM read_parquet('s3://{MINIO_BUCKET}/embeddings/**/*.parquet')
            ORDER BY score DESC
            LIMIT 5
        """
        con = duckdb.connect()
        rel = con.execute(sql)
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
            model="gemini-2.5-flash",
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
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("lake_token")
    return resp


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    stats = _get_bucket_stats()
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "stats": stats, "page": "home"},
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
    return templates.TemplateResponse(
        "catalog.html",
        {"request": request, "catalogs": catalogs, "page": "catalog"},
    )


@app.post("/catalog/create")
async def catalog_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    workspace_id: str = Form(""),
):
    token = request.cookies.get("lake_token")
    # Prefer the live session workspace over the form field — the form value is
    # baked in at page-render time and can be stale (cached page, back-nav, etc.).
    ws = request.state.workspace
    effective_workspace_id = (str(ws["id"]) if ws else None) or workspace_id or None
    if token:
        _backend_post(
            "/api/catalogs",
            token,
            {
                "name": name,
                "description": description or None,
                "workspace_id": effective_workspace_id,
            },
        )
    return RedirectResponse(url="/catalog", status_code=302)


@app.post("/catalog/refresh-schema/{table_id}")
async def catalog_refresh_schema(request: Request, table_id: str):
    # placeholder — schema refresh is triggered via the nightly DAG or manually
    return RedirectResponse(url="/catalog", status_code=302)


@app.get("/storage", response_class=HTMLResponse)
async def storage(request: Request):
    stats = _get_bucket_stats()
    return templates.TemplateResponse(
        "storage.html",
        {
            "request": request,
            "stats": stats,
            "page": "storage",
            "minio_console_url": MINIO_CONSOLE_URL,
        },
    )


@app.get("/sql", response_class=HTMLResponse)
async def sql_get(request: Request):
    return templates.TemplateResponse(
        "sql.html",
        {
            "request": request,
            "page": "sql",
            "result": None,
            "sql": EXAMPLE_QUERIES[0]["sql"],
            "example_queries": EXAMPLE_QUERIES,
        },
    )


@app.post("/sql", response_class=HTMLResponse)
async def sql_post(request: Request, sql: str = Form(...)):
    result = _run_query(sql)
    return templates.TemplateResponse(
        "sql.html",
        {
            "request": request,
            "page": "sql",
            "result": result,
            "sql": sql,
            "example_queries": EXAMPLE_QUERIES,
        },
    )


@app.get("/dashboards", response_class=HTMLResponse)
async def dashboards(request: Request):
    return templates.TemplateResponse(
        "embed.html",
        {
            "request": request,
            "page": "dashboards",
            "embed_url": SUPERSET_URL,
            "embed_title": "Visualizations",
            "embed_service": "Apache Superset",
        },
    )


@app.get("/pipelines", response_class=HTMLResponse)
async def pipelines(request: Request):
    ws = request.state.workspace
    embed_url = f"/airflow/?tags={ws['slug']}" if ws else "/airflow/"
    return templates.TemplateResponse(
        "embed.html",
        {
            "request": request,
            "page": "pipelines",
            "embed_url": embed_url,
            "embed_title": "Pipelines",
            "embed_service": "Apache Airflow",
        },
    )


@app.get("/notebooks", response_class=HTMLResponse)
async def notebooks(request: Request):
    ws = request.state.workspace
    token_param = f"?token={JUPYTER_TOKEN}" if JUPYTER_TOKEN else ""
    embed_url = (
        f"/jupyter/lab/tree/{ws['slug']}/{token_param}" if ws else f"/jupyter/lab{token_param}"
    )
    return templates.TemplateResponse(
        "embed.html",
        {
            "request": request,
            "page": "notebooks",
            "embed_url": embed_url,
            "embed_title": "Notebooks",
            "embed_service": "JupyterLab",
        },
    )


@app.get("/ingest", response_class=HTMLResponse)
async def ingest(request: Request):
    stats = _get_bucket_stats()
    return templates.TemplateResponse(
        "ingest.html",
        {"request": request, "stats": stats, "page": "ingest"},
    )


@app.get("/ai", response_class=HTMLResponse)
async def ai_get(request: Request):
    return templates.TemplateResponse(
        "ai.html",
        {
            "request": request,
            "page": "ai",
            "result": None,
            "question": "",
            "example_questions": EXAMPLE_QUESTIONS,
        },
    )


@app.post("/ai", response_class=HTMLResponse)
async def ai_post(request: Request, question: str = Form(...)):
    result = _ai_query(question)
    return templates.TemplateResponse(
        "ai.html",
        {
            "request": request,
            "page": "ai",
            "result": result,
            "question": question,
            "example_questions": EXAMPLE_QUESTIONS,
        },
    )


@app.get("/anomalies", response_class=HTMLResponse)
async def anomalies(request: Request):
    return templates.TemplateResponse(
        "anomalies.html",
        {"request": request, "page": "anomalies"},
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


def _backend_post(path: str, token: str, json: dict) -> dict | None:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.post(
                f"{BACKEND_URL}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code in (200, 201):
            return resp.json()
    except Exception:
        pass
    return None


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
    return templates.TemplateResponse(
        "settings/account.html",
        {"request": request, "page": "settings_account", "saved": saved, "profile": profile},
    )


@app.post("/settings/account", response_class=HTMLResponse)
async def settings_account_post(request: Request, display_name: str = Form(...)):
    token = request.cookies.get("lake_token")
    if token:
        _backend_patch("/api/auth/me", token, {"display_name": display_name})
    return RedirectResponse(url="/settings/account?saved=1", status_code=302)


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
        "settings/workspace.html",
        {
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
    }
    return templates.TemplateResponse(
        "settings/admin.html",
        {
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
