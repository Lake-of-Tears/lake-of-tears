from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
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
    CatalogSchema,
    CatalogTable,
    User,
    Workspace,
    WorkspaceMember,
)

_spec = importlib.util.spec_from_file_location(
    "backend_main_vector_retrieve", _REPO_ROOT / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(_spec)
sys.modules["backend_main_vector_retrieve"] = backend_main
_spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)
DIMS = 8


def _unit_vec(idx: int) -> list[float]:
    v = [0.0] * DIMS
    v[idx % DIMS] = 1.0
    return v


def _blended_vec(idx_a: int, weight_a: float, idx_b: int, weight_b: float) -> list[float]:
    v = [0.0] * DIMS
    v[idx_a % DIMS] = weight_a
    v[idx_b % DIMS] = weight_b
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
                [src, "2026-11-09T15:00:00Z", content, vec, model],
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
        return SimpleNamespace(text=f"Grounded answer: {contents}")


@pytest.fixture()
def vector_rbac_env(tmp_path: Path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()

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
    session.add_all([admin_user, worker_user])
    session.commit()

    ws_exec = Workspace(
        id=uuid.uuid4(),
        name="Executive",
        slug="executive",
        status="active",
    )
    ws_train = Workspace(
        id=uuid.uuid4(),
        name="Training",
        slug="training",
        status="active",
    )
    session.add_all([ws_exec, ws_train])
    session.commit()

    session.add_all(
        [
            WorkspaceMember(workspace_id=ws_exec.id, user_id=admin_user.id, role="admin"),
            WorkspaceMember(workspace_id=ws_train.id, user_id=admin_user.id, role="admin"),
            WorkspaceMember(workspace_id=ws_train.id, user_id=worker_user.id, role="read"),
        ]
    )
    session.commit()

    exec_parquet = tmp_path / "exec_vectors.parquet"
    train_parquet = tmp_path / "train_vectors.parquet"

    _write_parquet_embeddings(
        exec_parquet,
        [
            (
                "executive.gold.mna_targets",
                "Confidential M&A target: Project Chimera valuation is $420M.",
                _unit_vec(1),
                "test-embed-v1",
            ),
            (
                "executive.silver.board_minutes",
                "Q4 Board Minutes: dividend strategy review.",
                _blended_vec(1, 0.6, 2, 0.8),
                "test-embed-v1",
            ),
        ],
    )
    _write_parquet_embeddings(
        train_parquet,
        [
            (
                "training.gold.onboarding_docs",
                "Employee onboarding module 1: warehouse safety orientation.",
                _unit_vec(1),
                "test-embed-v1",
            ),
            (
                "training.bronze.quiz_bank",
                "Quiz question 4: Forklift inspection checklist.",
                _blended_vec(1, 0.5, 3, 0.866),
                "test-embed-v1",
            ),
        ],
    )

    cat_exec = Catalog(
        id=uuid.uuid4(),
        name="executive",
        slug="executive",
        description="Confidential executive catalog",
        owner_workspace_id=ws_exec.id,
        is_default=True,
    )
    cat_train = Catalog(
        id=uuid.uuid4(),
        name="training",
        slug="training",
        description="Worker training catalog",
        owner_workspace_id=ws_train.id,
        is_default=True,
    )
    session.add_all([cat_exec, cat_train])
    session.commit()

    sch_exec_gold = CatalogSchema(
        id=uuid.uuid4(), catalog_id=cat_exec.id, name="gold", slug="gold", tier="gold"
    )
    sch_exec_silver = CatalogSchema(
        id=uuid.uuid4(), catalog_id=cat_exec.id, name="silver", slug="silver", tier="silver"
    )
    sch_train_gold = CatalogSchema(
        id=uuid.uuid4(), catalog_id=cat_train.id, name="gold", slug="gold", tier="gold"
    )
    sch_train_bronze = CatalogSchema(
        id=uuid.uuid4(), catalog_id=cat_train.id, name="bronze", slug="bronze", tier="bronze"
    )
    session.add_all([sch_exec_gold, sch_exec_silver, sch_train_gold, sch_train_bronze])
    session.commit()

    tbl_mna = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_exec_gold.id,
        name="mna_targets",
        slug="mna_targets",
        s3_path_pattern=str(exec_parquet),
        vector_index={
            "enabled": True,
            "status": "ready",
            "s3_vector_path": str(exec_parquet),
        },
    )
    tbl_board = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_exec_silver.id,
        name="board_minutes",
        slug="board_minutes",
        s3_path_pattern=str(exec_parquet),
        vector_index={
            "enabled": True,
            "status": "ready",
            "s3_vector_path": str(exec_parquet),
        },
    )
    tbl_onboard = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_train_gold.id,
        name="onboarding_docs",
        slug="onboarding_docs",
        s3_path_pattern=str(train_parquet),
        vector_index={
            "enabled": True,
            "status": "ready",
            "s3_vector_path": str(train_parquet),
        },
    )
    tbl_quiz = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_train_bronze.id,
        name="quiz_bank",
        slug="quiz_bank",
        s3_path_pattern=str(train_parquet),
        vector_index={
            "enabled": True,
            "status": "ready",
            "s3_vector_path": str(train_parquet),
        },
    )
    session.add_all([tbl_mna, tbl_board, tbl_onboard, tbl_quiz])
    session.commit()

    _, admin_pat = auth.create_personal_access_token(
        session,
        user_id=admin_user.id,
        name="admin-vec-pat",
        workspace_access={str(ws_exec.id): "admin", str(ws_train.id): "admin"},
        scopes=["read", "write", "admin"],
    )
    _, worker_pat = auth.create_personal_access_token(
        session,
        user_id=worker_user.id,
        name="worker-vec-pat",
        workspace_access={str(ws_train.id): "read"},
        scopes=["read"],
    )

    backend_main.app.dependency_overrides[database.get_db] = lambda: session

    try:
        yield {
            "session": session,
            "admin_pat": admin_pat,
            "worker_pat": worker_pat,
            "admin_user": admin_user,
            "worker_user": worker_user,
        }
    finally:
        backend_main.app.dependency_overrides.clear()
        session.close()


def test_retrieve_endpoint_admin_vs_worker_rbac(vector_rbac_env):
    stub_client = _StubProviderClient(_unit_vec(1))

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # 1. Admin retrieves chunks across all workspaces/catalogs
        r_admin = client.post(
            "/api/v1/retrieve",
            headers={"Authorization": f"Bearer {vector_rbac_env['admin_pat']}"},
            json={"query": "Show all M&A targets and training modules", "top_k": 10},
        )
        assert r_admin.status_code == 200
        body_admin = r_admin.json()
        assert body_admin["ok"] is True
        assert body_admin["model"] == "test-embed-v1"
        assert body_admin["dimension"] == DIMS
        admin_sources = {c["source"] for c in body_admin["results"]}
        assert "executive.gold.mna_targets" in admin_sources
        assert "training.gold.onboarding_docs" in admin_sources

        # Verify LangChain/LlamaIndex compatible fields and metadata
        first_chunk = body_admin["results"][0]
        assert first_chunk["text"] == first_chunk["page_content"]
        assert "workspace_slug" in first_chunk["metadata"]
        assert "catalog_slug" in first_chunk["metadata"]
        assert "schema_slug" in first_chunk["metadata"]
        assert "table_slug" in first_chunk["metadata"]

        # 2. Worker only retrieves chunks from authorized 'training' workspace
        r_worker = client.post(
            "/api/v1/retrieve",
            headers={"Authorization": f"Bearer {vector_rbac_env['worker_pat']}"},
            json={"query": "Show all M&A targets and training modules", "top_k": 10},
        )
        assert r_worker.status_code == 200
        body_worker = r_worker.json()
        worker_sources = {c["source"] for c in body_worker["results"]}
        assert "training.gold.onboarding_docs" in worker_sources
        assert "executive.gold.mna_targets" not in worker_sources
        assert "executive.silver.board_minutes" not in worker_sources

        # 3. Worker attempting to filter by restricted workspace, catalog, or table receives 403
        for forbidden_payload in (
            {"query": "test", "workspace_slug": "executive"},
            {"query": "test", "catalog": "executive"},
            {"query": "test", "table": "mna_targets"},
        ):
            r_forbid = client.post(
                "/api/v1/retrieve",
                headers={"Authorization": f"Bearer {vector_rbac_env['worker_pat']}"},
                json=forbidden_payload,
            )
            assert r_forbid.status_code == 403


def test_retrieve_filtering_by_catalog_schema_table_and_score_threshold(vector_rbac_env):
    stub_client = _StubProviderClient(_unit_vec(1))

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # Filter by schema_name="bronze"
        r_schema = client.post(
            "/api/v1/vector/search",
            headers={"Authorization": f"Bearer {vector_rbac_env['worker_pat']}"},
            json={
                "query": "Forklift inspection",
                "catalog": "training",
                "schema_name": "bronze",
                "top_k": 5,
            },
        )
        assert r_schema.status_code == 200
        results_schema = r_schema.json()["results"]
        assert len(results_schema) == 1
        assert results_schema[0]["source"] == "training.bronze.quiz_bank"
        assert results_schema[0]["metadata"]["schema_slug"] == "bronze"
        assert results_schema[0]["metadata"]["table_slug"] == "quiz_bank"

        # Filter by score_threshold=0.85 (only exact unit_vec(1) match has score 1.0; blended has 0.5/0.6)
        r_thresh = client.post(
            "/api/ai/retrieve",
            headers={"Authorization": f"Bearer {vector_rbac_env['admin_pat']}"},
            json={
                "query": "High similarity only",
                "score_threshold": 0.85,
                "top_k": 10,
            },
        )
        assert r_thresh.status_code == 200
        results_thresh = r_thresh.json()["results"]
        assert len(results_thresh) >= 2
        assert all(item["score"] >= 0.85 for item in results_thresh)
        thresh_sources = {item["source"] for item in results_thresh}
        assert thresh_sources == {"executive.gold.mna_targets", "training.gold.onboarding_docs"}


def test_openai_vector_stores_list_and_search_rbac(vector_rbac_env):
    stub_client = _StubProviderClient(_unit_vec(1))

    # 1. List vector_stores as Admin vs Worker
    r_list_admin = client.get(
        "/api/v1/vector_stores",
        headers={"Authorization": f"Bearer {vector_rbac_env['admin_pat']}"},
    )
    assert r_list_admin.status_code == 200
    admin_vs_ids = {vs["id"] for vs in r_list_admin.json()["data"]}
    assert "all" in admin_vs_ids
    assert "executive" in admin_vs_ids
    assert "training" in admin_vs_ids
    assert "executive:executive.gold.mna_targets" in admin_vs_ids
    assert "training:training.gold.onboarding_docs" in admin_vs_ids

    r_list_worker = client.get(
        "/api/v1/vector_stores",
        headers={"Authorization": f"Bearer {vector_rbac_env['worker_pat']}"},
    )
    assert r_list_worker.status_code == 200
    worker_vs_ids = {vs["id"] for vs in r_list_worker.json()["data"]}
    assert "training" in worker_vs_ids
    assert "training:training.gold.onboarding_docs" in worker_vs_ids
    assert "all" not in worker_vs_ids
    assert "executive" not in worker_vs_ids
    assert "executive:executive.gold.mna_targets" not in worker_vs_ids

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # 2. Search table-scoped vector store as Worker
        r_search_tbl = client.post(
            "/api/v1/vector_stores/training:training.gold.onboarding_docs/search",
            headers={"Authorization": f"Bearer {vector_rbac_env['worker_pat']}"},
            json={
                "query": "onboarding safety",
                "max_num_results": 5,
                "ranking_options": {"score_threshold": 0.8},
            },
        )
        assert r_search_tbl.status_code == 200
        page = r_search_tbl.json()
        assert page["object"] == "vector_store.search_results.page"
        assert len(page["data"]) == 1
        item = page["data"][0]
        assert item["file_id"] == "training.gold.onboarding_docs"
        assert item["attributes"]["workspace_slug"] == "training"
        assert item["attributes"]["catalog"] == "training"
        assert item["attributes"]["schema"] == "gold"
        assert item["attributes"]["table"] == "onboarding_docs"
        assert "warehouse safety orientation" in item["content"][0]["text"]

        # 3. Worker attempting to search executive vector store gets 403 Forbidden
        r_search_forbid = client.post(
            "/api/v1/vector_stores/executive:executive.gold.mna_targets/search",
            headers={"Authorization": f"Bearer {vector_rbac_env['worker_pat']}"},
            json={"query": "Project Chimera"},
        )
        assert r_search_forbid.status_code == 403

        # 4. Admin PAT with X-On-Behalf-Of worker is also restricted to training
        r_search_impersonated = client.post(
            "/api/v1/vector_stores/all/search",
            headers={
                "Authorization": f"Bearer {vector_rbac_env['admin_pat']}",
                "X-On-Behalf-Of": "worker@lake.local",
            },
            json={"query": "Show all documents"},
        )
        assert r_search_impersonated.status_code == 200
        imp_files = {d["file_id"] for d in r_search_impersonated.json()["data"]}
        assert "training.gold.onboarding_docs" in imp_files
        assert "executive.gold.mna_targets" not in imp_files
