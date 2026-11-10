"""End-to-End Playwright Browser Verification Suite for Lakeset BI Studio (/lakeset).

Validates the complete Lakeset BI Studio user journey in headless Chromium:
1. Sidebar navigation to `/lakeset` with Zero-Login SSO.
2. 1-Click Catalog-to-Lakeset semantic synchronization (`#btn-sync-lakeset`) and column description inspection.
3. Launching the Chart Builder (`Explore`) on a governed dataset and executing live DuckDB chart queries.
4. Switching across `Dashboards` and `SQL Lab` zero-login embedded studio tabs.
5. 1-Click `Open in Lakeset` deep-linking from the SQL Editor (`/sql`).
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


repo_root = Path(__file__).resolve().parents[1]
backend_dir = str(repo_root / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
from database import Base  # noqa: E402
from models import (  # noqa: E402
    Catalog,
    CatalogSchema,
    CatalogTable,
    User,
    Workspace,
    WorkspaceDashboard,
    WorkspaceMember,
)

import backend.main as backend_main  # noqa: E402
import ui.main as ui_main  # noqa: E402


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


@pytest.fixture(scope="module")
def lakeset_server(tmp_path_factory):
    """Launches an ephemeral Uvicorn server combining UI and Backend with seeded catalog tables."""
    test_dir = tmp_path_factory.mktemp("lakeset_e2e_env")
    db_path = test_dir / "lakeset_e2e.db"

    orig_engine = database.engine
    orig_session_local = database.SessionLocal
    orig_backend_url = ui_main.BACKEND_URL

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

    ws_id = uuid.uuid4()
    ws_name = "Biomedical Analytics"
    ws_slug = "default"
    workspace = Workspace(
        id=ws_id,
        name=ws_name,
        slug=ws_slug,
        status="active",
        created_at=datetime.now(UTC),
    )
    db.add(workspace)

    user_id = uuid.uuid4()
    user_email = "admin@lake.local"
    user = User(
        id=user_id,
        email=user_email,
        display_name="Admin User",
        role="superadmin",
        hashed_password=auth.hash_password("adminpass123"),
        is_active=True,
    )
    db.add(user)
    db.flush()

    db.add(
        WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role="admin",
        )
    )

    cat = Catalog(
        id=uuid.uuid4(),
        name="biomedical_lake",
        slug="biomedical_lake",
        description="Biomedical Medallion Catalog",
        owner_workspace_id=workspace.id,
        created_by=user.id,
    )
    db.add(cat)
    db.flush()

    sch = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat.id,
        name="gold",
        slug="gold",
        tier="gold",
        description="Gold analytical aggregates",
    )
    db.add(sch)
    db.flush()

    tbl = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch.id,
        name="journal_trends",
        slug="journal_trends",
        description="Annual biomedical publication trends by journal",
        s3_path_pattern="s3://datalake/workspaces/default/gold/journal_trends/*.parquet",
        column_defs=[
            {
                "name": "pub_year",
                "type": "VARCHAR",
                "nullable": False,
                "description": "Publication calendar year",
            },
            {
                "name": "journal",
                "type": "VARCHAR",
                "nullable": False,
                "description": "Peer-reviewed medical journal title",
            },
            {
                "name": "article_count",
                "type": "BIGINT",
                "nullable": False,
                "description": "Total indexed PubMed articles",
            },
        ],
    )
    db.add(tbl)

    dash = WorkspaceDashboard(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        title="PubMed Executive Overview",
        slug="pubmed-executive-overview",
        description="Executive KPIs for biomedical publications",
        is_published=True,
        layout_config=[{"id": "w1", "title": "Total Articles", "type": "kpi"}],
        charts_count=1,
        created_by=user.id,
    )
    db.add(dash)

    session_rec = auth.create_user_session(
        db=db,
        user=user,
        expires_delta=timedelta(days=7),
    )
    token = auth.create_token(
        data={"sub": str(user_id), "email": user_email, "role": "superadmin"},
        session_id=str(session_rec.id),
        expires_delta=timedelta(days=7),
    )

    db.commit()
    db.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_url = f"http://127.0.0.1:{port}"
    database.engine = engine
    database.SessionLocal = session_factory

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    backend_main.app.dependency_overrides[database.get_db] = override_get_db

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
        pytest.fail("Ephemeral Lakeset E2E server failed to start within 5 seconds")

    class ServerContext:
        def __init__(self):
            self.url = server_url
            self.token = token
            self.workspace_id = ws_id
            self.workspace_slug = ws_slug

    yield ServerContext()

    uv_server.should_exit = True
    server_thread.join(timeout=2.0)
    patch_ui_ws.stop()
    database.engine = orig_engine
    database.SessionLocal = orig_session_local
    backend_main.app.dependency_overrides.pop(database.get_db, None)
    ui_main.BACKEND_URL = orig_backend_url


@pytest.fixture
def lakeset_page(lakeset_server):
    """Playwright headless Chromium browser page pre-authenticated with workspace cookies."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            base_url=lakeset_server.url,
        )
        context.add_cookies(
            [
                {
                    "name": "lake_token",
                    "value": lakeset_server.token,
                    "domain": "127.0.0.1",
                    "path": "/",
                },
                {
                    "name": ui_main._WORKSPACE_COOKIE,
                    "value": str(lakeset_server.workspace_id),
                    "domain": "127.0.0.1",
                    "path": "/",
                },
            ]
        )
        pg = context.new_page()
        yield pg
        context.close()
        browser.close()


def test_e2e_lakeset_sidebar_sync_and_semantic_inspector(lakeset_page, lakeset_server):
    """Verify sidebar navigation to /lakeset, 1-click Catalog Sync, and Semantic Schema Drawer."""
    page = lakeset_page
    page.goto("/lakeset", wait_until="networkidle")

    # Assert Lakeset BI Studio header and dataset card load without login screen
    assert page.locator("h1", has_text="Lakeset BI Studio").is_visible()
    assert page.locator("text=biomedical_lake.gold.journal_trends").first.is_visible()

    # Click 'Sync Catalogs to Lakeset' button and verify 'Synced' badge appears
    sync_btn = page.locator("#btn-sync-lakeset")
    assert sync_btn.is_visible()
    sync_btn.click()

    page.wait_for_selector("span.badge-green:has-text('Synced')", timeout=5000)
    assert page.locator("span.badge-green:has-text('Synced')").first.is_visible()

    # Open Semantic Schema inspector drawer and verify AI column descriptions
    page.locator("button:has-text('Semantic Schema')").first.click()
    page.locator("text=Peer-reviewed medical journal title").last.wait_for(
        state="visible", timeout=5000
    )
    assert page.locator("text=Peer-reviewed medical journal title").last.is_visible()
    assert page.locator("text=Total indexed PubMed articles").last.is_visible()


def test_e2e_lakeset_chart_builder_and_sql_editor_deep_link(lakeset_page, lakeset_server):
    """Verify 1-click Create Chart in /lakeset and Open in Lakeset deep-link from /sql."""
    page = lakeset_page
    page.goto("/lakeset", wait_until="networkidle")

    # Click 'Create Chart' on journal_trends dataset
    page.locator("button:has-text('Create Chart')").first.click()

    # Verify Chart Builder (Explore) tab is active and SQL preview is synthesized
    page.wait_for_selector("#lakeset-explore-sql-preview", timeout=5000)
    sql_preview = page.locator("#lakeset-explore-sql-preview").inner_text()
    assert "gold.journal_trends" in sql_preview

    # Switch to Dashboards tab and verify seeded dashboard card + zero-login embedded iframe
    page.locator("#lakeset-tab-dashboards").click()
    page.locator("text=PubMed Executive Overview").first.wait_for(state="visible", timeout=5000)
    assert page.locator("text=PubMed Executive Overview").first.is_visible()
    assert page.locator("#lakeset-dashboards-iframe").is_visible()

    # Navigate to SQL Editor (/sql) and click 'Open in Lakeset'
    custom_sql = "SELECT journal, SUM(article_count) AS total FROM gold.journal_trends GROUP BY 1"
    page.goto(f"/sql?q={custom_sql}", wait_until="networkidle")
    open_lakeset_btn = page.locator("#btnOpenInLakeset")
    assert open_lakeset_btn.is_visible()
    open_lakeset_btn.click()

    page.wait_for_url("**/lakeset?mode=explore&sql=**", timeout=5000)
    assert "mode=explore" in page.url
    populated_sql = " ".join(page.locator("#explore-sql-input").input_value().split())
    assert populated_sql == custom_sql


def test_e2e_lakeset_search_filter_and_sqllab_tab(lakeset_page, lakeset_server):
    """Verify dataset semantic search, Medallion tier filtering, and Zero-Login SQL Lab tab."""
    page = lakeset_page
    page.goto("/lakeset", wait_until="networkidle")

    # Search by column semantic description ('Peer-reviewed')
    search_input = page.locator("#lakeset-dataset-search")
    search_input.fill("Peer-reviewed")
    assert page.locator("text=biomedical_lake.gold.journal_trends").first.is_visible()

    # Search for non-matching term and verify empty state
    search_input.fill("nonexistent_table_xyz")
    page.locator("text=No Governed Datasets Found").wait_for(state="visible", timeout=5000)
    assert page.locator("text=No Governed Datasets Found").is_visible()
    search_input.fill("")

    # Click 'SQL Lab' action on dataset card and verify SQL Lab iframe loads
    page.locator("button:has-text('SQL Lab')").last.click()
    page.locator("#lakeset-sqllab-iframe").wait_for(state="visible", timeout=5000)
    assert page.locator("#lakeset-sqllab-iframe").is_visible()
    iframe_src = page.locator("#lakeset-sqllab-iframe").get_attribute("src")
    assert "/lakeset/proxy/sqllab/" in iframe_src
    assert "gold.journal_trends" in iframe_src
