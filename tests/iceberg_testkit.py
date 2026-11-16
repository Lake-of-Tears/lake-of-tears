"""Shared fixture for the Iceberg test modules: an in-memory catalog DB, a local-filesystem
warehouse and a PyIceberg REST client wired straight into the FastAPI app."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from pyiceberg.catalog.rest import RestCatalog
from requests.adapters import BaseAdapter
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

_REPO_ROOT = Path(__file__).resolve().parent.parent
backend_dir = str(_REPO_ROOT / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    Catalog,
    CatalogSchema,
    User,
    Workspace,
    WorkspaceMember,
)

_spec = importlib.util.spec_from_file_location(
    "backend_main_iceberg", _REPO_ROOT / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(_spec)
sys.modules["backend_main_iceberg"] = backend_main
_spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)
BASE = "http://lot.test"


class _TestClientAdapter(BaseAdapter):
    """Route PyIceberg's `requests` traffic into the in-process FastAPI app."""

    def send(self, request, **kwargs):  # noqa: ANN001
        resp = client.request(
            request.method,
            request.url[len(BASE) :],
            headers=dict(request.headers),
            content=request.body,
        )
        out = requests.Response()
        out.status_code = resp.status_code
        out._content = resp.content
        out.headers.update(resp.headers)
        out.url = request.url
        out.request = request
        return out

    def close(self):
        pass


class _InProcessRestCatalog(RestCatalog):
    def _create_session(self) -> requests.Session:
        session = super()._create_session()
        session.mount(BASE, _TestClientAdapter())
        return session


@pytest.fixture(name="env")
def iceberg_env(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[database.get_db] = _get_db
    monkeypatch.setenv("ICEBERG_WAREHOUSE_ROOT", f"file://{tmp_path}/wh")

    db = Session()
    ws = Workspace(id=uuid.uuid4(), name="Dev", slug="dev", status="active")
    other_ws = Workspace(id=uuid.uuid4(), name="Other", slug="other", status="active")
    writer = User(id=uuid.uuid4(), email="writer@lake.local", role="user", is_active=True)
    reader = User(id=uuid.uuid4(), email="reader@lake.local", role="user", is_active=True)
    outsider = User(id=uuid.uuid4(), email="outsider@lake.local", role="user", is_active=True)
    db.add_all([ws, other_ws, writer, reader, outsider])
    db.flush()
    db.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=writer.id, role="write"),
            WorkspaceMember(workspace_id=ws.id, user_id=reader.id, role="read"),
            WorkspaceMember(workspace_id=other_ws.id, user_id=outsider.id, role="admin"),
        ]
    )
    cat = Catalog(id=uuid.uuid4(), name="Main Lake", slug="main_lake", owner_workspace_id=ws.id)
    foreign = Catalog(
        id=uuid.uuid4(), name="Foreign", slug="foreign", owner_workspace_id=other_ws.id
    )
    db.add_all([cat, foreign])
    db.flush()
    db.add_all(
        [
            CatalogSchema(catalog_id=cat.id, name="bronze", slug="bronze", tier="bronze"),
            CatalogSchema(catalog_id=foreign.id, name="bronze", slug="bronze", tier="bronze"),
        ]
    )
    db.commit()

    def _token(user: User) -> str:
        session = auth.create_user_session(db, user, timedelta(hours=1))
        return auth.create_token({"sub": str(user.id)}, session_id=session.id)

    def _rest(user: User, warehouse: str = "dev") -> RestCatalog:
        token = _token(user)
        return _InProcessRestCatalog(
            "lot", uri=f"{BASE}/api/v1/iceberg", warehouse=warehouse, token=token
        )

    yield {
        "client": client,
        "cat": cat,
        "db": db,
        "Session": Session,
        "ws": ws,
        "writer": writer,
        "reader": reader,
        "outsider": outsider,
        "rest": _rest,
        "token": _token,
        "root": tmp_path / "wh",
    }
    db.close()
    backend_main.app.dependency_overrides.pop(database.get_db, None)
