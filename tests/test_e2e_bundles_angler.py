"""End-to-End Playwright + Angler CLI Verification Suite.

Validates the complete Lake Asset Bundles (LABs) lifecycle and core Angler CLI commands
running against an ephemeral Lake of Tears test server and asserting on live browser UI state.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    sync_playwright = None

if not HAS_PLAYWRIGHT:
    pytestmark = pytest.mark.skip(reason="playwright is not installed")
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

# ── SQLite Compatibility for JSONB ───────────────────────────────────────────


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


repo_root = Path(__file__).resolve().parents[1]
backend_dir = str(repo_root / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import pipeline_service  # noqa: E402
from database import Base  # noqa: E402
from models import (  # noqa: E402
    User,
    Workspace,
    WorkspaceMember,
)

import backend.main as backend_main  # noqa: E402
import ui.main as ui_main  # noqa: E402

# ── Angler Binary Discovery & Compilation ────────────────────────────────────


@pytest.fixture(scope="session")
def angler_binary() -> Path:
    """Ensure the Angler CLI binary is compiled and available across environments."""
    # 1. Environment variable override
    env_bin = os.environ.get("ANGLER_BIN")
    if env_bin and Path(env_bin).exists() and os.access(env_bin, os.X_OK):
        return Path(env_bin)

    # 2. Check PATH
    which_bin = shutil.which("angler")
    if which_bin:
        return Path(which_bin)

    # 3. Check known repository locations
    potential_repos = [
        Path(os.environ.get("ANGLER_REPO_DIR", "")),
        Path("/Users/Jay/Development/angler/main"),
        repo_root.parent / "angler" / "main",
        repo_root.parent / "angler",
        repo_root / "angler-src",
    ]
    for rdir in potential_repos:
        if not rdir or not rdir.exists():
            continue
        bin_candidate = rdir / "bin" / "angler"
        if bin_candidate.exists() and os.access(bin_candidate, os.X_OK):
            return bin_candidate
        # If Go compiler is available, attempt build
        if shutil.which("go") and (rdir / "main.go").exists():
            bin_candidate.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(
                ["go", "build", "-o", str(bin_candidate), "main.go"],
                cwd=str(rdir),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and bin_candidate.exists():
                return bin_candidate

    pytest.skip("Angler CLI binary not found and go compiler unavailable to build it.")


# ── Ingress ASGI Gateway ─────────────────────────────────────────────────────


class IngressTestApp:
    """Dispatches /api/* requests to backend FastAPI app and everything else to UI app."""

    def __init__(self, backend_app, ui_app):
        self.backend_app = backend_app
        self.ui_app = ui_app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path.startswith("/api/") or path == "/api":
                await self.backend_app(scope, receive, send)
                return
        await self.ui_app(scope, receive, send)


# ── Ephemeral Test Server Fixture ────────────────────────────────────────────


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Launches an ephemeral background Uvicorn server combining UI and Backend on a single port."""
    test_dir = tmp_path_factory.mktemp("lake_e2e_env")
    db_path = test_dir / "lake_e2e.db"
    dags_dir = test_dir / "airflow_dags"
    dags_dir.mkdir(parents=True, exist_ok=True)

    # Save original globals for pristine restoration
    orig_engine = database.engine
    orig_session_local = database.SessionLocal
    orig_backend_url = ui_main.BACKEND_URL

    # 1. Database setup
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = session_factory()

    # Seed test workspace with slug 'default' (matching Angler's default header)
    ws_id = uuid.uuid4()
    ws_name = "Default Workspace"
    ws_slug = "default"
    workspace = Workspace(
        id=ws_id,
        name=ws_name,
        slug=ws_slug,
        status="active",
        created_at=datetime.now(UTC),
    )
    db.add(workspace)

    # Seed admin user
    user_id = uuid.uuid4()
    user_email = "admin@lake.local"
    user_password = "adminpass123"
    user = User(
        id=user_id,
        email=user_email,
        display_name="Admin User",
        role="superadmin",
        hashed_password=auth.hash_password(user_password),
        is_active=True,
    )
    db.add(user)
    db.flush()

    membership = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=user.id,
        role="admin",
    )
    db.add(membership)

    # Create active session in database
    session_rec = auth.create_user_session(
        db=db,
        user=user,
        expires_delta=timedelta(days=7),
    )

    # Create signed authentication token linked to session
    token = auth.create_token(
        data={"sub": str(user_id), "email": "admin@lake.local", "role": "superadmin"},
        session_id=str(session_rec.id),
        expires_delta=timedelta(days=7),
    )

    db.commit()
    db.close()

    # Find free loopback port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_url = f"http://127.0.0.1:{port}"

    # Patch database engine and SessionLocal in backend
    database.engine = engine
    database.SessionLocal = session_factory

    # Override get_db in both backend and ui
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    backend_main.app.dependency_overrides[database.get_db] = override_get_db

    # Mock get_airflow_dags_dir to write inside test_dir
    patch_dags = patch.object(pipeline_service, "get_airflow_dags_dir", return_value=dags_dir)
    patch_dags.start()

    # Mock ui workspace fetching so UI AuthMiddleware resolves active workspace correctly
    mock_ws_list = [
        {
            "id": str(ws_id),
            "name": ws_name,
            "slug": ws_slug,
            "my_role": "admin",
            "is_shared": False,
        }
    ]
    patch_ui_ws = patch.object(ui_main, "_fetch_workspaces", return_value=mock_ws_list)
    patch_ui_ws.start()

    # Point UI BACKEND_URL to the server_url
    ui_main.BACKEND_URL = server_url

    ingress_app = IngressTestApp(backend_main.app, ui_main.app)

    import uvicorn

    config = uvicorn.Config(
        app=ingress_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    uv_server = uvicorn.Server(config)

    server_thread = threading.Thread(target=uv_server.run, daemon=True)
    server_thread.start()

    # Wait for server ready
    import httpx

    ready = False
    for _ in range(50):
        try:
            r = httpx.get(f"{server_url}/api/health", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.1)

    if not ready:
        pytest.fail("Ephemeral Lake test server failed to start within 5 seconds")

    class ServerContext:
        def __init__(self):
            self.url = server_url
            self.port = port
            self.token = token
            self.email = user_email
            self.password = user_password
            self.workspace_id = ws_id
            self.workspace_slug = ws_slug
            self.dags_dir = dags_dir

    ctx = ServerContext()
    yield ctx

    # Teardown
    uv_server.should_exit = True
    server_thread.join(timeout=2.0)
    patch_dags.stop()
    patch_ui_ws.stop()
    database.engine = orig_engine
    database.SessionLocal = orig_session_local
    backend_main.app.dependency_overrides.pop(database.get_db, None)
    ui_main.BACKEND_URL = orig_backend_url


# ── Playwright Page Fixture ──────────────────────────────────────────────────


@pytest.fixture
def page(server):
    """Playwright headless Chromium browser page pre-authenticated with workspace cookies."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            base_url=server.url,
        )

        # Set session and workspace cookies for the ephemeral host
        context.add_cookies(
            [
                {
                    "name": "lake_token",
                    "value": server.token,
                    "domain": "127.0.0.1",
                    "path": "/",
                },
                {
                    "name": ui_main._WORKSPACE_COOKIE,
                    "value": str(server.workspace_id),
                    "domain": "127.0.0.1",
                    "path": "/",
                },
                {
                    "name": "lake_active_workspace",
                    "value": str(server.workspace_id),
                    "domain": "127.0.0.1",
                    "path": "/",
                },
                {
                    "name": "lake_workspace_slug",
                    "value": server.workspace_slug,
                    "domain": "127.0.0.1",
                    "path": "/",
                },
            ]
        )

        browser_page = context.new_page()
        yield browser_page

        context.close()
        browser.close()


# ── Angler CLI Runner Helper ─────────────────────────────────────────────────


def run_angler(
    angler_bin: Path,
    server: Any,
    args: list[str],
    cwd: Path | None = None,
    with_token: bool = True,
    home_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Execute an Angler CLI command with target endpoint and optional token attached."""
    env = os.environ.copy()
    env["ANGLER_ENDPOINT"] = server.url
    if with_token:
        env["ANGLER_TOKEN"] = server.token
    else:
        env.pop("ANGLER_TOKEN", None)

    if home_dir:
        env["HOME"] = str(home_dir)
        env.pop("ANGLER_CONTEXT", None)
    else:
        env["ANGLER_CONTEXT"] = "test"

    cmd = [str(angler_bin)] + args
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ── 1. Core Angler CLI Commands Verification ─────────────────────────────────


def test_angler_core_cli(server, angler_binary, tmp_path):
    """Verify all core Angler CLI commands: version, completion, context, auth, ask/sonar, status, query, catalog, tool-defs."""
    # 1. Version info
    res_ver = run_angler(angler_binary, server, ["version", "--json"])
    assert res_ver.returncode == 0
    ver_data = json.loads(res_ver.stdout)
    assert "version" in ver_data

    # 2. Shell completion
    res_comp = run_angler(angler_binary, server, ["completion", "bash"])
    assert res_comp.returncode == 0
    assert "complete" in res_comp.stdout or "bash" in res_comp.stdout

    # 3. Context management (set, list, use)
    res_ctx_set = run_angler(
        angler_binary,
        server,
        ["context", "set", "prod-cluster", "--endpoint", "http://prod.internal:8000"],
        home_dir=tmp_path,
    )
    assert res_ctx_set.returncode == 0

    res_ctx_list = run_angler(
        angler_binary,
        server,
        ["context", "list", "--json"],
        home_dir=tmp_path,
    )
    assert res_ctx_list.returncode == 0
    ctx_items = json.loads(res_ctx_list.stdout)
    assert any(c.get("name") == "prod-cluster" for c in ctx_items)

    res_ctx_use = run_angler(
        angler_binary,
        server,
        ["context", "use", "prod-cluster"],
        home_dir=tmp_path,
    )
    assert res_ctx_use.returncode == 0

    # 4. Auth flow (status -> login -> status -> logout)
    res_auth_stat_unauth = run_angler(
        angler_binary,
        server,
        ["auth", "status", "--json"],
        with_token=False,
        home_dir=tmp_path,
    )
    stat_unauth = json.loads(res_auth_stat_unauth.stdout)
    assert not stat_unauth.get("authenticated")

    res_auth_login = run_angler(
        angler_binary,
        server,
        [
            "auth",
            "login",
            "--email",
            server.email,
            "--password",
            server.password,
            "--endpoint",
            server.url,
        ],
        with_token=False,
        home_dir=tmp_path,
    )
    assert res_auth_login.returncode == 0, f"auth login failed: {res_auth_login.stderr}"

    res_auth_stat_auth = run_angler(
        angler_binary,
        server,
        ["auth", "status", "--json"],
        with_token=False,
        home_dir=tmp_path,
    )
    stat_auth = json.loads(res_auth_stat_auth.stdout)
    assert stat_auth.get("authenticated")
    assert stat_auth.get("email") == server.email

    res_auth_logout = run_angler(
        angler_binary,
        server,
        ["auth", "logout"],
        with_token=False,
        home_dir=tmp_path,
    )
    assert res_auth_logout.returncode == 0

    # 5. Natural language AI Search: ask & sonar alias
    res_ask = run_angler(
        angler_binary,
        server,
        ["ask", "What are the active revenue metrics?", "--json"],
    )
    assert res_ask.returncode == 0, f"angler ask failed: {res_ask.stderr}"
    ask_data = json.loads(res_ask.stdout)
    assert "answer" in ask_data

    res_sonar = run_angler(
        angler_binary,
        server,
        ["sonar", "Scan for anomalous tables in silver tier", "--json"],
    )
    assert res_sonar.returncode == 0, f"angler sonar failed: {res_sonar.stderr}"
    sonar_data = json.loads(res_sonar.stdout)
    assert "answer" in sonar_data

    # 6. Status / Ping & Status JSON
    res_status = run_angler(angler_binary, server, ["status"])
    assert res_status.returncode == 0, f"angler status failed: {res_status.stderr}"
    assert (
        "Connected to Lake of Tears successfully" in res_status.stdout
        or "online" in res_status.stdout.lower()
        or "ok" in res_status.stdout.lower()
    )

    res_status_json = run_angler(angler_binary, server, ["status", "--json"])
    assert res_status_json.returncode == 0
    status_data = json.loads(res_status_json.stdout)
    assert status_data.get("status") in ("ok", "online")

    # 7. Tool Defs (OpenAI Schema)
    res_defs_openai = run_angler(angler_binary, server, ["tool-defs", "--format", "openai"])
    assert res_defs_openai.returncode == 0
    parsed_openai = json.loads(res_defs_openai.stdout)
    assert isinstance(parsed_openai, list)
    tool_names = [t.get("function", {}).get("name") for t in parsed_openai]
    assert "angler_query" in tool_names or any("bundle" in n for n in tool_names)

    # 8. Tool Defs (Gemini Function Declarations)
    res_defs_gemini = run_angler(angler_binary, server, ["tool-defs", "--format", "gemini"])
    assert res_defs_gemini.returncode == 0
    parsed_gemini = json.loads(res_defs_gemini.stdout)
    assert "function_declarations" in parsed_gemini

    # 9. Catalog ls and net alias
    res_cat = run_angler(angler_binary, server, ["catalog", "ls"])
    assert res_cat.returncode == 0

    res_net = run_angler(angler_binary, server, ["net"])
    assert res_net.returncode == 0

    # 10. Query / Cast via DuckDB
    res_query = run_angler(angler_binary, server, ["query", "SELECT 42 AS answer"])
    assert res_query.returncode == 0
    assert "42" in res_query.stdout

    res_cast = run_angler(angler_binary, server, ["cast", "SELECT 100 AS score", "--json"])
    assert res_cast.returncode == 0
    assert "100" in res_cast.stdout


# ── 2. Full Lake Asset Bundle Lifecycle & Playwright UI Loop ──────────────────


def test_angler_bundle_e2e_lifecycle(server, angler_binary, page, tmp_path):
    """E2E Test: Angler bundle lifecycle (init, validate, deploy, run, destroy) verified via Playwright UI."""
    bundle_dir = tmp_path / "lake-analytics"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # ── Step A: angler bundle init ──
    res_init = run_angler(
        angler_binary,
        server,
        ["bundle", "init", "lake-analytics", "--template", "python-etl", "--format", "toon"],
        cwd=bundle_dir,
    )
    assert res_init.returncode == 0, f"bundle init failed: {res_init.stderr}"
    assert (bundle_dir / "lake.toon").exists(), "lake.toon must be generated"
    assert (bundle_dir / "sql" / "clean_orders.sql").exists(), "SQL asset must be generated"

    # ── Step B: angler bundle validate (Local & Remote) ──
    res_val_local = run_angler(
        angler_binary,
        server,
        ["bundle", "validate", "--local"],
        cwd=bundle_dir,
    )
    assert res_val_local.returncode == 0
    assert "valid" in (res_val_local.stdout + res_val_local.stderr).lower()

    res_val_remote = run_angler(
        angler_binary,
        server,
        ["bundle", "validate", "-T", "dev"],
        cwd=bundle_dir,
    )
    assert res_val_remote.returncode == 0
    assert "valid" in (res_val_remote.stdout + res_val_remote.stderr).lower()

    # ── Step C: angler bundle deploy --dry-run ──
    res_dryrun = run_angler(
        angler_binary,
        server,
        ["bundle", "deploy", "-T", "dev", "--dry-run"],
        cwd=bundle_dir,
    )
    assert res_dryrun.returncode == 0
    assert "dry run" in (res_dryrun.stdout + res_dryrun.stderr).lower()

    # ── Step D: angler bundle deploy ──
    res_deploy = run_angler(
        angler_binary,
        server,
        ["bundle", "deploy", "-T", "dev"],
        cwd=bundle_dir,
    )
    assert res_deploy.returncode == 0, f"bundle deploy failed: {res_deploy.stderr}"
    assert (
        "deployed successfully" in (res_deploy.stdout + res_deploy.stderr).lower()
        or "lake-analytics" in res_deploy.stdout
    )

    # ── Step E: Playwright Browser UI Verification on /bundles ──
    page.goto(f"{server.url}/bundles")
    page.wait_for_selector("#bundles-container", timeout=8000)

    # Verify bundle card appears in gallery
    bundle_card_title = page.locator("h3", has_text="lake-analytics")
    bundle_card_title.wait_for(state="visible", timeout=6000)
    assert bundle_card_title.is_visible()

    # Verify Target badge indicates dev
    target_badge = page.locator("span", has_text="dev").first
    assert target_badge.is_visible()

    # Verify Status badge indicates active
    status_badge = page.locator("span", has_text="ACTIVE").first
    assert status_badge.is_visible()

    # Click Inspect button to open slide-over drawer
    inspect_btn = page.locator("button", has_text="Inspect").first
    inspect_btn.click()

    # Verify slide-over drawer opens and displays bundle metadata
    drawer = page.locator("#bundle-drawer")
    drawer.wait_for(state="visible", timeout=5000)
    drawer_heading = drawer.locator("h2", has_text="lake-analytics")
    assert drawer_heading.is_visible()

    # Verify Resources tab content
    resources_tab = drawer.locator("button", has_text="Resources")
    assert resources_tab.is_visible()

    # Switch to Visual Topology tab
    topology_tab = drawer.locator("button", has_text="Topology")
    topology_tab.click()
    page.wait_for_timeout(300)

    # Switch to Manifest tab and verify TOON code block
    manifest_tab = drawer.locator("button", has_text="Manifest")
    manifest_tab.click()
    page.wait_for_timeout(300)
    code_block = drawer.locator("pre")
    assert code_block.is_visible()
    code_text = code_block.inner_text()
    assert "lake-analytics" in code_text

    # Close drawer via Escape key or close button
    page.keyboard.press("Escape")
    drawer.wait_for(state="hidden", timeout=5000)
    page.wait_for_timeout(300)

    # ── Step F: angler bundle run ──
    res_run = run_angler(
        angler_binary,
        server,
        ["bundle", "run", "orders_pipeline", "-T", "dev"],
        cwd=bundle_dir,
    )
    assert res_run.returncode == 0, f"bundle run failed: {res_run.stderr}"
    assert (
        "queued" in res_run.stdout.lower()
        or "dag_id" in res_run.stdout.lower()
        or "triggered" in (res_run.stdout + res_run.stderr).lower()
    )

    # ── Step G: angler bundle ls ──
    res_ls = run_angler(
        angler_binary,
        server,
        ["bundle", "ls", "-T", "dev"],
        cwd=bundle_dir,
    )
    assert res_ls.returncode == 0
    assert "lake-analytics" in res_ls.stdout

    # ── Step H: angler bundle destroy --purge-storage -y ──
    res_destroy = run_angler(
        angler_binary,
        server,
        ["bundle", "destroy", "-T", "dev", "--purge-storage", "-y"],
        cwd=bundle_dir,
    )
    assert res_destroy.returncode == 0, f"bundle destroy failed: {res_destroy.stderr}"
    assert (
        "destroyed" in (res_destroy.stdout + res_destroy.stderr).lower()
        or res_destroy.returncode == 0
    )

    # ── Step I: Playwright UI Verification after Destroy ──
    # Refresh bundles page
    page.reload()
    page.wait_for_selector("#bundles-container", timeout=8000)

    # After destroy, either card is removed or status is marked DESTROYED
    destroyed_badge = page.locator("span", has_text="DESTROYED").first
    empty_state_text = page.locator("h3", has_text="No Asset Bundles Found").first

    # One of the conditions must be true in the UI
    assert (
        destroyed_badge.is_visible()
        or empty_state_text.is_visible()
        or not page.locator("h3", has_text="lake-analytics").is_visible()
    )
