from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

_REPO_ROOT = Path(__file__).resolve().parent.parent
backend_dir = str(_REPO_ROOT / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import ai_service  # noqa: E402
import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    Catalog,
    CatalogAccess,
    CatalogSchema,
    CatalogTable,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
)

_spec = importlib.util.spec_from_file_location(
    "backend_main_openai_compat", _REPO_ROOT / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(_spec)
sys.modules["backend_main_openai_compat"] = backend_main
_spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)
DIMS = 8


def _vec(seed: float) -> list[float]:
    v = [0.0] * DIMS
    idx = int(seed) % DIMS
    v[idx] = 1.0
    return v


def _write_parquet_embeddings(path: Path, rows: list[tuple[str, str, list[float], str]]) -> None:
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            CREATE TABLE emb (
                source VARCHAR,
                ingested_at VARCHAR,
                content VARCHAR,
                embedding DOUBLE[{DIMS}],
                model VARCHAR
            )
            """
        )
        for src, content, vec, model in rows:
            con.execute(
                "INSERT INTO emb VALUES (?, ?, ?, ?, ?)",
                [src, "2026-11-09T12:00:00Z", content, vec, model],
            )
        con.execute("COPY emb TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        con.close()


class _StubProviderClient:
    def __init__(self, query_vec: list[float]):
        self._query_vec = query_vec
        self.models = self

    def embed_content(self, model: str | None = None, contents=None, config=None):
        items = [contents] if isinstance(contents, str) else list(contents or [""])
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=self._query_vec) for _ in items],
            model="test-embed-v1",
            dimension=DIMS,
        )

    def generate_content(self, model: str | None = None, contents: str = "", config=None):
        return SimpleNamespace(text=f"Grounded answer from context: {contents}")


@pytest.fixture()
def gateway_env(tmp_path: Path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()

    now = datetime.now(UTC)

    # Create users: superadmin & scoped training worker & inactive user
    admin_user = User(
        id=uuid.uuid4(),
        email="admin@lake.local",
        display_name="Chief Admin",
        role="superadmin",
        is_active=True,
    )
    worker_user = User(
        id=uuid.uuid4(),
        email="worker@lake.local",
        display_name="Training Worker",
        role="user",
        is_active=True,
    )
    inactive_user = User(
        id=uuid.uuid4(),
        email="inactive@lake.local",
        display_name="Inactive User",
        role="user",
        is_active=False,
    )
    session.add_all([admin_user, worker_user, inactive_user])
    session.commit()

    # Sessions & JWTs
    admin_sess = UserSession(
        id=uuid.uuid4(),
        user_id=admin_user.id,
        created_at=now,
        last_active_at=now,
        expires_at=now + timedelta(hours=24),
        is_revoked=False,
    )
    worker_sess = UserSession(
        id=uuid.uuid4(),
        user_id=worker_user.id,
        created_at=now,
        last_active_at=now,
        expires_at=now + timedelta(hours=24),
        is_revoked=False,
    )
    session.add_all([admin_sess, worker_sess])
    session.commit()

    admin_jwt = jwt.encode(
        {
            "sub": str(admin_user.id),
            "email": admin_user.email,
            "role": admin_user.role,
            "jti": str(admin_sess.id),
        },
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    worker_jwt = jwt.encode(
        {
            "sub": str(worker_user.id),
            "email": worker_user.email,
            "role": worker_user.role,
            "jti": str(worker_sess.id),
        },
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    # Workspaces: 'executive' (restricted) and 'training' (worker-accessible)
    ws_exec = Workspace(
        id=uuid.uuid4(),
        name="Executive Vault",
        slug="executive",
        status="active",
        created_by=admin_user.id,
    )
    ws_training = Workspace(
        id=uuid.uuid4(),
        name="Training Academy",
        slug="training",
        status="active",
        created_by=admin_user.id,
    )
    session.add_all([ws_exec, ws_training])
    session.commit()

    # Memberships: admin in both, worker ONLY in training
    session.add_all(
        [
            WorkspaceMember(workspace_id=ws_exec.id, user_id=admin_user.id, role="admin"),
            WorkspaceMember(workspace_id=ws_training.id, user_id=admin_user.id, role="admin"),
            WorkspaceMember(workspace_id=ws_training.id, user_id=worker_user.id, role="read"),
        ]
    )
    session.commit()

    # Personal Access Tokens
    _, admin_pat = auth.create_personal_access_token(
        session,
        user_id=admin_user.id,
        name="LibreChat Service Proxy PAT",
        workspace_access={str(ws_exec.id): "admin", str(ws_training.id): "admin"},
        scopes=["read", "write", "admin"],
    )
    _, worker_pat = auth.create_personal_access_token(
        session,
        user_id=worker_user.id,
        name="Worker Training PAT",
        workspace_access={str(ws_training.id): "read"},
        scopes=["read"],
    )

    # Write local Parquet vector index files for executive vs training
    exec_parquet = tmp_path / "exec_embeddings.parquet"
    training_parquet = tmp_path / "training_embeddings.parquet"
    _write_parquet_embeddings(
        exec_parquet,
        [
            (
                "executive.gold.mna_targets",
                "CONFIDENTIAL: Acquisition target Project Chimera valued at $450M.",
                _vec(1),
                "test-embed-v1",
            )
        ],
    )
    _write_parquet_embeddings(
        training_parquet,
        [
            (
                "training.gold.onboarding_guide",
                "TRAINING: All new hires must complete safety orientation module 101.",
                _vec(1),
                "test-embed-v1",
            )
        ],
    )

    # Catalogs, Schemas, and Vector-Indexed Tables
    cat_exec = Catalog(
        id=uuid.uuid4(),
        name="executive",
        slug="executive",
        owner_workspace_id=ws_exec.id,
        created_by=admin_user.id,
        is_default=True,
    )
    cat_training = Catalog(
        id=uuid.uuid4(),
        name="training",
        slug="training",
        owner_workspace_id=ws_training.id,
        created_by=admin_user.id,
        is_default=True,
    )
    session.add_all([cat_exec, cat_training])
    session.commit()

    sch_exec = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat_exec.id,
        name="gold",
        slug="gold",
        tier="gold",
    )
    sch_training = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat_training.id,
        name="gold",
        slug="gold",
        tier="gold",
    )
    session.add_all([sch_exec, sch_training])
    session.commit()

    tbl_exec = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_exec.id,
        name="mna_targets",
        slug="mna_targets",
        s3_path_pattern=str(exec_parquet),
        vector_index={
            "enabled": True,
            "status": "synced",
            "s3_vector_path": str(exec_parquet),
        },
    )
    tbl_training = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_training.id,
        name="onboarding_guide",
        slug="onboarding_guide",
        s3_path_pattern=str(training_parquet),
        vector_index={
            "enabled": True,
            "status": "synced",
            "s3_vector_path": str(training_parquet),
        },
    )
    session.add_all([tbl_exec, tbl_training])
    session.commit()

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        yield {
            "session": session,
            "admin_user": admin_user,
            "worker_user": worker_user,
            "admin_jwt": admin_jwt,
            "worker_jwt": worker_jwt,
            "admin_pat": admin_pat,
            "worker_pat": worker_pat,
            "ws_exec": ws_exec,
            "ws_training": ws_training,
            "exec_parquet": str(exec_parquet),
            "training_parquet": str(training_parquet),
        }
    finally:
        backend_main.app.dependency_overrides.clear()
        session.close()


def test_list_models_rbac_filtering(gateway_env):
    # 1. Admin sees base models + both executive and training workspace models
    resp_admin = client.get(
        "/api/v1/models",
        headers={"Authorization": f"Bearer {gateway_env['admin_pat']}"},
    )
    assert resp_admin.status_code == 200
    admin_ids = {m["id"] for m in resp_admin.json()["data"]}
    assert {"lot-rag", "lot-genie", "lot-hybrid"} <= admin_ids
    assert "lot-rag:executive" in admin_ids
    assert "lot-genie:executive" in admin_ids
    assert "lot-rag:training" in admin_ids
    assert "lot-genie:training" in admin_ids

    # 2. Worker PAT only sees base models + training workspace models (never executive)
    resp_worker = client.get(
        "/api/v1/models",
        headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
    )
    assert resp_worker.status_code == 200
    worker_ids = {m["id"] for m in resp_worker.json()["data"]}
    assert {"lot-rag", "lot-genie", "lot-hybrid"} <= worker_ids
    assert "lot-rag:training" in worker_ids
    assert "lot-genie:training" in worker_ids
    assert "lot-rag:executive" not in worker_ids
    assert "lot-genie:executive" not in worker_ids

    # 3. Single model lookup enforces RBAC
    assert (
        client.get(
            "/api/v1/models/lot-rag:training",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/models/lot-rag:executive",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
        ).status_code
        == 404
    )


def test_trusted_proxy_impersonation_x_on_behalf_of(gateway_env):
    # 1. Admin PAT impersonating worker@lake.local via X-On-Behalf-Of only sees training models
    resp_imp = client.get(
        "/api/v1/models",
        headers={
            "Authorization": f"Bearer {gateway_env['admin_pat']}",
            "X-On-Behalf-Of": "worker@lake.local",
        },
    )
    assert resp_imp.status_code == 200
    imp_ids = {m["id"] for m in resp_imp.json()["data"]}
    assert "lot-rag:training" in imp_ids
    assert "lot-rag:executive" not in imp_ids

    # 2. Also works via X-Lot-User-Email header and X-API-Key header
    resp_email_hdr = client.get(
        "/api/ai/v1/models",
        headers={
            "X-API-Key": gateway_env["admin_pat"],
            "X-Lot-User-Email": "worker@lake.local",
        },
    )
    assert resp_email_hdr.status_code == 200
    assert "lot-rag:executive" not in {m["id"] for m in resp_email_hdr.json()["data"]}

    # 3. Non-superadmin attempting to impersonate admin gets 403 Forbidden
    resp_forbidden = client.get(
        "/api/v1/models",
        headers={
            "Authorization": f"Bearer {gateway_env['worker_pat']}",
            "X-On-Behalf-Of": "admin@lake.local",
        },
    )
    assert resp_forbidden.status_code == 403

    # 4. Superadmin attempting to impersonate non-existent or inactive user gets 403 Forbidden
    resp_missing = client.get(
        "/api/v1/models",
        headers={
            "Authorization": f"Bearer {gateway_env['admin_pat']}",
            "X-On-Behalf-Of": "ghost@lake.local",
        },
    )
    assert resp_missing.status_code == 403

    resp_inactive = client.get(
        "/api/v1/models",
        headers={
            "Authorization": f"Bearer {gateway_env['admin_pat']}",
            "X-On-Behalf-Of": "inactive@lake.local",
        },
    )
    assert resp_inactive.status_code == 403


def test_chat_completions_rbac_vector_isolation_admin_vs_worker(gateway_env):
    stub_client = _StubProviderClient(_vec(1))

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # 1. Admin querying 'lot-rag' retrieves embeddings across BOTH executive and training
        resp_admin = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['admin_pat']}"},
            json={
                "model": "lot-rag",
                "messages": [{"role": "user", "content": "What projects and training exist?"}],
                "stream": False,
            },
        )
        assert resp_admin.status_code == 200
        body_admin = resp_admin.json()
        assert body_admin["object"] == "chat.completion"
        admin_sources = {s["source"] for s in body_admin["lot_metadata"]["snippets"]}
        assert "executive.gold.mna_targets" in admin_sources
        assert "training.gold.onboarding_guide" in admin_sources
        assert "Project Chimera" in body_admin["choices"][0]["message"]["content"]

        # 2. Worker querying 'lot-rag' ONLY retrieves training embeddings (never executive!)
        resp_worker = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "lot-rag",
                "messages": [{"role": "user", "content": "What projects and training exist?"}],
                "stream": False,
            },
        )
        assert resp_worker.status_code == 200
        body_worker = resp_worker.json()
        worker_sources = {s["source"] for s in body_worker["lot_metadata"]["snippets"]}
        assert worker_sources == {"training.gold.onboarding_guide"}
        assert "executive.gold.mna_targets" not in worker_sources
        assert "Project Chimera" not in body_worker["choices"][0]["message"]["content"]
        assert "safety orientation" in body_worker["choices"][0]["message"]["content"]

        # 3. Admin token with X-On-Behalf-Of: worker@lake.local is also strictly isolated to training
        resp_proxy_worker = client.post(
            "/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {gateway_env['admin_pat']}",
                "X-On-Behalf-Of": "worker@lake.local",
            },
            json={
                "model": "lot-rag",
                "messages": [{"role": "user", "content": "Tell me everything"}],
                "stream": False,
            },
        )
        assert resp_proxy_worker.status_code == 200
        proxy_sources = {s["source"] for s in resp_proxy_worker.json()["lot_metadata"]["snippets"]}
        assert proxy_sources == {"training.gold.onboarding_guide"}
        assert "executive.gold.mna_targets" not in proxy_sources

        # 4. Worker attempting to query lot-rag:executive or executive catalog gets 403 Forbidden
        resp_unauth_ws = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "lot-rag:executive",
                "messages": [{"role": "user", "content": "Show executive targets"}],
            },
        )
        assert resp_unauth_ws.status_code == 403

        resp_unauth_cat = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "lot-rag",
                "catalog_filter": "executive",
                "messages": [{"role": "user", "content": "Show executive targets"}],
            },
        )
        assert resp_unauth_cat.status_code == 403


def test_chat_completions_streaming_sse(gateway_env):
    stub_client = _StubProviderClient(_vec(1))

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        with client.stream(
            "POST",
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "lot-rag:training",
                "messages": [{"role": "user", "content": "What is the onboarding module?"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            raw_lines = [line for line in resp.iter_lines() if line]

    assert raw_lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in raw_lines[:-1] if line.startswith("data: ")]
    assert len(chunks) >= 2
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    full_text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "safety orientation" in full_text
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["lot_metadata"]["workspace_slug"] == "training"


def test_openai_embeddings_endpoint(gateway_env):
    stub_client = _StubProviderClient(_vec(2))

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
    ):
        resp = client.post(
            "/api/v1/embeddings",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "text-embedding-004",
                "input": ["First sentence", "Second sentence"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 2
        assert data["data"][0]["object"] == "embedding"
        assert data["data"][0]["index"] == 0
        assert len(data["data"][0]["embedding"]) == DIMS


def test_chat_completions_approved_catalog_share_and_genie(gateway_env):
    stub_client = _StubProviderClient(_vec(1))
    session = gateway_env["session"]

    # Grant worker approved CatalogAccess to executive catalog in training workspace
    exec_cat = session.query(Catalog).filter_by(name="executive").first()
    train_ws = session.query(Workspace).filter_by(slug="training").first()
    worker_user = session.query(User).filter_by(email="worker@lake.local").first()
    access = CatalogAccess(
        id=uuid.uuid4(),
        catalog_id=exec_cat.id,
        workspace_id=train_ws.id,
        mode="read",
        status="approved",
        requested_by=worker_user.id,
    )
    session.add(access)
    session.commit()

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # Worker can now retrieve embeddings from the shared executive catalog too
        resp_shared = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "lot-rag:training",
                "catalog_filter": "executive",
                "messages": [{"role": "user", "content": "What M&A targets are shared?"}],
            },
        )
        assert resp_shared.status_code == 200
        body_shared = resp_shared.json()
        shared_sources = {s["source"] for s in body_shared["lot_metadata"]["snippets"]}
        assert "executive.gold.mna_targets" in shared_sources

        # Test lot-genie mode
        resp_genie = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {gateway_env['worker_pat']}"},
            json={
                "model": "lot-genie:training",
                "messages": [{"role": "user", "content": "Summarize onboarding modules"}],
            },
        )
        assert resp_genie.status_code == 200
        body_genie = resp_genie.json()
        assert body_genie["lot_metadata"]["mode"] == "genie"
        assert body_genie["lot_metadata"]["workspace_slug"] == "training"
