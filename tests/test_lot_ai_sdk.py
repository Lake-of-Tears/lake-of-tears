from __future__ import annotations

import asyncio
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
import lake_of_tears  # noqa: E402
import lot  # noqa: E402
from lot import (  # noqa: E402
    LakeOfTearsClient,
    LakeOfTearsDocument,
    LakeOfTearsRetriever,
    LakeOfTearsToolkit,
)
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
    "backend_main_lot_ai_sdk", _REPO_ROOT / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(_spec)
sys.modules["backend_main_lot_ai_sdk"] = backend_main
_spec.loader.exec_module(backend_main)

test_http_client = TestClient(backend_main.app)
DIMS = 8


def _unit_vec(idx: int) -> list[float]:
    v = [0.0] * DIMS
    v[idx % DIMS] = 1.0
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
                [src, "2026-11-09T17:00:00Z", content, vec, model],
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
        return SimpleNamespace(text=f"SDK Grounded response: {contents}")


@pytest.fixture()
def sdk_env(tmp_path: Path):
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

    exec_parquet = tmp_path / "exec_sdk.parquet"
    train_parquet = tmp_path / "train_sdk.parquet"

    _write_parquet_embeddings(
        exec_parquet,
        [
            (
                "executive.gold.mna_targets",
                "Confidential M&A target: Project Chimera valuation is $420M.",
                _unit_vec(1),
                "test-embed-v1",
            )
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
            )
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
    sch_train_gold = CatalogSchema(
        id=uuid.uuid4(), catalog_id=cat_train.id, name="gold", slug="gold", tier="gold"
    )
    session.add_all([sch_exec_gold, sch_train_gold])
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
    session.add_all([tbl_mna, tbl_onboard])
    session.commit()

    _, admin_pat = auth.create_personal_access_token(
        session,
        user_id=admin_user.id,
        name="admin-sdk-pat",
        workspace_access={str(ws_exec.id): "admin", str(ws_train.id): "admin"},
        scopes=["read", "write", "admin"],
    )
    _, worker_pat = auth.create_personal_access_token(
        session,
        user_id=worker_user.id,
        name="worker-sdk-pat",
        workspace_access={str(ws_train.id): "read"},
        scopes=["read"],
    )

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        yield {
            "admin_pat": admin_pat,
            "worker_pat": worker_pat,
        }
    finally:
        backend_main.app.dependency_overrides.clear()
        session.close()


def test_sdk_exports_and_header_construction():
    assert lake_of_tears.LakeOfTearsClient is LakeOfTearsClient
    assert lot.LakeOfTearsRetriever is LakeOfTearsRetriever
    assert lot.LakeOfTearsToolkit is LakeOfTearsToolkit
    assert lot.LakeOfTearsDocument is LakeOfTearsDocument

    c = LakeOfTearsClient(
        base_url="http://localhost:8000/api/v1/",
        api_key="lot_pat_test123",
        workspace_slug="training",
        on_behalf_of="worker@lake.local",
    )
    assert c.base_url == "http://localhost:8000"
    headers = c._build_headers()
    assert headers["Authorization"] == "Bearer lot_pat_test123"
    assert headers["X-Workspace-Slug"] == "training"
    assert headers["X-On-Behalf-Of"] == "worker@lake.local"

    scoped = c.with_user(on_behalf_of="other@lake.local", workspace_slug="executive")
    scoped_headers = scoped._build_headers()
    assert scoped_headers["X-On-Behalf-Of"] == "other@lake.local"
    assert scoped_headers["X-Workspace-Slug"] == "executive"


def test_retriever_invoke_and_aretrieve_with_per_user_rbac(sdk_env):
    stub_client = _StubProviderClient(_unit_vec(1))
    lot_client = LakeOfTearsClient(
        api_key=sdk_env["admin_pat"],
        http_client=test_http_client,
    )
    retriever = LakeOfTearsRetriever(client=lot_client, top_k=5)

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # 1. Admin retriever invocation returns documents from both executive and training
        admin_docs = retriever.invoke("What documents exist?")
        assert len(admin_docs) == 2
        assert all(isinstance(d, LakeOfTearsDocument) for d in admin_docs)
        admin_sources = {d.metadata["source"] for d in admin_docs}
        assert admin_sources == {"executive.gold.mna_targets", "training.gold.onboarding_docs"}

        # 2. Dynamic per-call LangChain RunnableConfig on_behalf_of scopes to worker ('training' only)
        worker_docs = retriever.invoke(
            {"query": "What documents exist?"},
            config={"configurable": {"on_behalf_of": "worker@lake.local"}},
        )
        assert len(worker_docs) == 1
        assert worker_docs[0].metadata["source"] == "training.gold.onboarding_docs"
        assert worker_docs[0].metadata["catalog"] == "training"
        assert worker_docs[0].metadata["schema"] == "gold"
        assert worker_docs[0].metadata["table"] == "onboarding_docs"
        assert "warehouse safety orientation" in worker_docs[0].text

        # 3. Async retrieval (aretrieve) with worker PAT
        worker_retriever = LakeOfTearsRetriever(
            api_key=sdk_env["worker_pat"],
            http_client=test_http_client,
        )
        async_docs = asyncio.run(worker_retriever.aretrieve("safety orientation"))
        assert len(async_docs) == 1
        assert async_docs[0].metadata["source"] == "training.gold.onboarding_docs"

        # 4. Worker attempting to retrieve from executive catalog raises PermissionError
        with pytest.raises(PermissionError, match="403"):
            worker_retriever.get_relevant_documents("Project Chimera", catalog="executive")


def test_toolkit_methods_and_rbac_enforcement(sdk_env):
    stub_client = _StubProviderClient(_unit_vec(1))
    worker_toolkit = LakeOfTearsToolkit(
        api_key=sdk_env["admin_pat"],
        on_behalf_of="worker@lake.local",
        http_client=test_http_client,
    )
    tools = worker_toolkit.get_tools()
    assert len(tools) == 4

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="provider"),
        patch.object(ai_service, "_client_for", return_value=stub_client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: duckdb.connect(),
        ),
    ):
        # list_tables only returns training catalog for impersonated worker
        tables_info = worker_toolkit.list_tables()
        cat_slugs = {c["slug"] for c in tables_info["catalogs"]}
        assert cat_slugs == {"training"}

        # search_datalake only returns training chunks
        chunks = worker_toolkit.search_datalake("Show all chunks")
        assert len(chunks) == 1
        assert chunks[0]["source"] == "training.gold.onboarding_docs"

        # query_sql executes read-only DuckDB query on training table
        sql_res = worker_toolkit.query_sql(
            "SELECT source, content FROM training.gold.onboarding_docs"
        )
        assert sql_res["ok"] is True
        assert sql_res["row_count"] == 1

        # query_sql against executive workspace raises PermissionError (403)
        with pytest.raises(PermissionError, match="403"):
            worker_toolkit.query_sql(
                "SELECT * FROM executive.gold.mna_targets",
                workspace_slug="executive",
            )
