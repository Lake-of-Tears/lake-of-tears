"""Shared fixtures and utilities for Angler CLI end-to-end tests."""

from __future__ import annotations

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
import storage_service  # noqa: E402
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


class ServerContext:
    def __init__(
        self,
        url: str,
        port: int,
        token: str,
        email: str,
        password: str,
        workspace_id: uuid.UUID,
        workspace_slug: str,
        dags_dir: Path,
        test_dir: Path,
        warehouse_dir: Path,
        session_factory: Any,
    ):
        self.url = url
        self.port = port
        self.token = token
        self.email = email
        self.password = password
        self.workspace_id = workspace_id
        self.workspace_slug = workspace_slug
        self.dags_dir = dags_dir
        self.test_dir = test_dir
        self.warehouse_dir = warehouse_dir
        self.session_factory = session_factory


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Launches an ephemeral background Uvicorn server combining UI and Backend on a single port."""
    test_dir = tmp_path_factory.mktemp("lake_e2e_env")
    db_path = test_dir / "lake_e2e.db"
    dags_dir = test_dir / "airflow_dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    wh_dir = test_dir / "warehouse"
    wh_dir.mkdir(parents=True, exist_ok=True)

    orig_wh_root = os.environ.get("ICEBERG_WAREHOUSE_ROOT")
    os.environ["ICEBERG_WAREHOUSE_ROOT"] = f"file://{wh_dir}"

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

    @event.listens_for(session_factory, "loaded_as_persistent")
    def _ensure_utc_tzinfo(session, instance):
        if hasattr(instance, "__table__"):
            for col in instance.__table__.columns:
                val = getattr(instance, col.name, None)
                if isinstance(val, datetime) and val.tzinfo is None:
                    setattr(instance, col.name, val.replace(tzinfo=UTC))

    db = session_factory()

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

    session_rec = auth.create_user_session(
        db=db,
        user=user,
        expires_delta=timedelta(days=7),
    )

    token = auth.create_token(
        data={"sub": str(user_id), "email": "admin@lake.local", "role": "superadmin"},
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

    patch_dags = patch.object(pipeline_service, "get_airflow_dags_dir", return_value=dags_dir)
    patch_dags.start()

    mock_dags = [
        {
            "dag_id": "e2e_sample_pipeline",
            "is_paused": False,
            "description": "E2E sample pipeline",
            "schedule_interval": "@daily",
            "next_dagrun": None,
            "has_import_errors": False,
        }
    ]
    patch_list_dags = patch.object(pipeline_service, "list_workspace_dags", return_value=mock_dags)
    patch_list_dags.start()

    def mock_browse_prefix(prefix="", limit=100, continuation_token=None, workspace_slug="default"):
        return {
            "bucket": "lake-of-tears",
            "workspace": workspace_slug,
            "prefix": prefix,
            "rel_prefix": prefix,
            "folders": [
                {
                    "name": "uploads",
                    "prefix": "uploads/",
                    "full_prefix": f"workspaces/{workspace_slug}/uploads/",
                }
            ],
            "files": [
                {
                    "key": f"workspaces/{workspace_slug}/uploads/hello.txt",
                    "rel_key": "uploads/hello.txt",
                    "name": "hello.txt",
                    "size": 33,
                    "size_formatted": "33 B",
                    "etag": "fake-etag",
                    "is_parquet": False,
                    "is_notebook": False,
                }
            ],
            "total_folders": 1,
            "total_files": 1,
            "is_truncated": False,
            "next_continuation_token": None,
        }

    patch_browse = patch.object(storage_service, "browse_prefix", side_effect=mock_browse_prefix)
    patch_browse.start()

    def mock_stream_object(bucket=None, key="", s3_client=None, workspace_slug="default"):
        def gen():
            yield b"Hello from Angler CLI test suite!"

        headers = {
            "Content-Type": "text/plain",
            "Content-Length": "33",
            "Content-Disposition": 'attachment; filename="hello.txt"',
        }
        return gen(), headers

    patch_stream = patch.object(storage_service, "stream_object", side_effect=mock_stream_object)
    patch_stream.start()

    def mock_upload_object(key="", data=b"", content_type="text/plain", workspace_slug="default", **kwargs):
        return {
            "key": f"workspaces/{workspace_slug}/{key.lstrip('/')}",
            "size": len(data) if data else 33,
            "content_type": content_type,
        }

    patch_upload = patch.object(storage_service, "upload_object", side_effect=mock_upload_object)
    patch_upload.start()

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
        pytest.fail("Ephemeral Lake test server failed to start within 5 seconds")

    ctx = ServerContext(
        url=server_url,
        port=port,
        token=token,
        email=user_email,
        password=user_password,
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        dags_dir=dags_dir,
        test_dir=test_dir,
        warehouse_dir=wh_dir,
        session_factory=session_factory,
    )
    yield ctx

    uv_server.should_exit = True
    server_thread.join(timeout=2.0)
    patch_dags.stop()
    patch_list_dags.stop()
    patch_browse.stop()
    patch_stream.stop()
    patch_upload.stop()
    patch_ui_ws.stop()
    database.engine = orig_engine
    database.SessionLocal = orig_session_local
    backend_main.app.dependency_overrides.pop(database.get_db, None)
    ui_main.BACKEND_URL = orig_backend_url
    if orig_wh_root is not None:
        os.environ["ICEBERG_WAREHOUSE_ROOT"] = orig_wh_root
    else:
        os.environ.pop("ICEBERG_WAREHOUSE_ROOT", None)


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
