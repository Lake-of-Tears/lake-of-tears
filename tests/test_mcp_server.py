from __future__ import annotations

import importlib.util
import json
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
    "backend_main_mcp_server", _REPO_ROOT / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(_spec)
sys.modules["backend_main_mcp_server"] = backend_main
_spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)
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
                [src, "2026-11-09T16:00:00Z", content, vec, model],
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
        if '"sql"' in contents:
            return SimpleNamespace(
                text='{"sql": "SELECT source, content FROM gold.onboarding_docs LIMIT 5", "tables_used": ["onboarding_docs"], "reasoning": "Query onboarding_docs"}'
            )
        return SimpleNamespace(text=f"Genie analytical summary: {contents}")


@pytest.fixture()
def mcp_env(tmp_path: Path):
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

    exec_parquet = tmp_path / "exec_mcp.parquet"
    train_parquet = tmp_path / "train_mcp.parquet"

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
        description="Executive M&A pipeline targets",
        column_defs=[
            {"name": "source", "type": "VARCHAR", "description": "Table source"},
            {"name": "content", "type": "VARCHAR", "description": "Deal memo"},
        ],
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
        description="Training onboarding documentation",
        column_defs=[
            {"name": "source", "type": "VARCHAR", "description": "Doc source"},
            {"name": "content", "type": "VARCHAR", "description": "Module body"},
        ],
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
        name="admin-mcp-pat",
        workspace_access={str(ws_exec.id): "admin", str(ws_train.id): "admin"},
        scopes=["read", "write", "admin"],
    )
    _, worker_pat = auth.create_personal_access_token(
        session,
        user_id=worker_user.id,
        name="worker-mcp-pat",
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


def _mcp_rpc(
    token: str,
    method: str,
    params: dict | None = None,
    req_id: int | None = 1,
    extra_headers: dict[str, str] | None = None,
):
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        payload["id"] = req_id
    if params is not None:
        payload["params"] = params
    return client.post("/api/mcp", headers=headers, json=payload)


def test_mcp_protocol_lifecycle_initialize_tools_and_resources(mcp_env):
    # 1. initialize
    r_init = _mcp_rpc(
        mcp_env["worker_pat"],
        "initialize",
        {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}},
    )
    assert r_init.status_code == 200
    init_res = r_init.json()["result"]
    assert init_res["protocolVersion"] == "2024-11-05"
    assert init_res["serverInfo"]["name"] == "lake-of-tears-mcp"
    assert "tools" in init_res["capabilities"]
    assert "resources" in init_res["capabilities"]

    # 2. notifications/initialized (no id -> 202 Accepted)
    r_notif = _mcp_rpc(mcp_env["worker_pat"], "notifications/initialized", req_id=None)
    assert r_notif.status_code == 202

    # 3. ping
    r_ping = _mcp_rpc(mcp_env["worker_pat"], "ping")
    assert r_ping.status_code == 200
    assert r_ping.json()["result"] == {}

    # 4. tools/list
    r_tools = _mcp_rpc(mcp_env["worker_pat"], "tools/list")
    assert r_tools.status_code == 200
    tool_names = {t["name"] for t in r_tools.json()["result"]["tools"]}
    assert tool_names == {
        "list_workspaces_and_catalogs",
        "describe_table",
        "semantic_search_datalake",
        "query_datalake_sql",
        "ask_genie",
    }

    # 5. resources/list & resources/read RBAC
    r_res_admin = _mcp_rpc(mcp_env["admin_pat"], "resources/list")
    admin_uris = {r["uri"] for r in r_res_admin.json()["result"]["resources"]}
    assert "lot://catalogs/executive/executive" in admin_uris
    assert "lot://catalogs/training/training" in admin_uris

    r_res_worker = _mcp_rpc(mcp_env["worker_pat"], "resources/list")
    worker_uris = {r["uri"] for r in r_res_worker.json()["result"]["resources"]}
    assert worker_uris == {"lot://catalogs/training/training"}

    # Worker can read training catalog resource
    r_read_ok = _mcp_rpc(
        mcp_env["worker_pat"],
        "resources/read",
        {"uri": "lot://catalogs/training/training"},
    )
    assert r_read_ok.status_code == 200
    read_content = json.loads(r_read_ok.json()["result"]["contents"][0]["text"])
    assert read_content["slug"] == "training"

    # Worker reading executive catalog resource gets JSON-RPC RBAC error (-32003 / 403)
    r_read_forbid = _mcp_rpc(
        mcp_env["worker_pat"],
        "resources/read",
        {"uri": "lot://catalogs/executive/executive"},
    )
    assert r_read_forbid.status_code == 200
    err = r_read_forbid.json()["error"]
    assert err["code"] == -32003
    assert err["data"]["status_code"] == 403


def test_mcp_tools_rbac_isolation_across_all_5_tools(mcp_env):
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
        # Tool 1: list_workspaces_and_catalogs
        r_list_admin = _mcp_rpc(
            mcp_env["admin_pat"],
            "tools/call",
            {"name": "list_workspaces_and_catalogs", "arguments": {}},
        )
        admin_cat_slugs = {
            c["slug"] for c in r_list_admin.json()["result"]["structuredContent"]["catalogs"]
        }
        assert admin_cat_slugs == {"executive", "training"}

        r_list_worker = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {"name": "list_workspaces_and_catalogs", "arguments": {}},
        )
        worker_cat_slugs = {
            c["slug"] for c in r_list_worker.json()["result"]["structuredContent"]["catalogs"]
        }
        assert worker_cat_slugs == {"training"}

        # Tool 2: describe_table
        r_desc_ok = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {
                "name": "describe_table",
                "arguments": {
                    "catalog": "training",
                    "schema": "gold",
                    "table": "onboarding_docs",
                    "include_sample": True,
                },
            },
        )
        desc_res = r_desc_ok.json()["result"]
        assert desc_res["isError"] is False
        assert desc_res["structuredContent"]["table"] == "onboarding_docs"
        assert len(desc_res["structuredContent"]["sample_rows"]) == 1

        r_desc_forbid = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {
                "name": "describe_table",
                "arguments": {
                    "catalog": "executive",
                    "schema": "gold",
                    "table": "mna_targets",
                },
            },
        )
        desc_forbid_res = r_desc_forbid.json()["result"]
        assert desc_forbid_res["isError"] is True
        assert desc_forbid_res["structuredContent"]["status_code"] == 403

        # Tool 3: semantic_search_datalake
        r_sem_admin = _mcp_rpc(
            mcp_env["admin_pat"],
            "tools/call",
            {
                "name": "semantic_search_datalake",
                "arguments": {"query": "Show all documents", "top_k": 5},
            },
        )
        admin_sem_sources = {
            c["source"] for c in r_sem_admin.json()["result"]["structuredContent"]["results"]
        }
        assert admin_sem_sources == {"executive.gold.mna_targets", "training.gold.onboarding_docs"}

        r_sem_worker = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {
                "name": "semantic_search_datalake",
                "arguments": {"query": "Show all documents", "top_k": 5},
            },
        )
        worker_sem_sources = {
            c["source"] for c in r_sem_worker.json()["result"]["structuredContent"]["results"]
        }
        assert worker_sem_sources == {"training.gold.onboarding_docs"}

        # Tool 4: query_datalake_sql
        r_sql_ok = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {
                "name": "query_datalake_sql",
                "arguments": {"sql": "SELECT source, content FROM training.gold.onboarding_docs"},
            },
        )
        sql_ok_res = r_sql_ok.json()["result"]
        assert sql_ok_res["isError"] is False
        assert sql_ok_res["structuredContent"]["row_count"] == 1

        # Write query blocked by read-only guardrail
        r_sql_write = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {
                "name": "query_datalake_sql",
                "arguments": {"sql": "DROP TABLE gold.onboarding_docs"},
            },
        )
        assert r_sql_write.json()["result"]["isError"] is True

        # Cross-workspace S3 path blocked by S3 guardrail (403)
        r_sql_cross_ws = _mcp_rpc(
            mcp_env["worker_pat"],
            "tools/call",
            {
                "name": "query_datalake_sql",
                "arguments": {
                    "sql": "SELECT * FROM read_parquet('s3://datalake/workspaces/executive/embeddings/**/*.parquet')"
                },
            },
        )
        sql_cross_res = r_sql_cross_ws.json()["result"]
        assert sql_cross_res["isError"] is True
        assert sql_cross_res["structuredContent"]["status_code"] == 403

        # Tool 5: ask_genie (with X-On-Behalf-Of worker impersonation)
        r_genie_imp = _mcp_rpc(
            mcp_env["admin_pat"],
            "tools/call",
            {
                "name": "ask_genie",
                "arguments": {
                    "question": "What is in onboarding?",
                    "mode": "rag",
                },
            },
            extra_headers={"X-On-Behalf-Of": "worker@lake.local"},
        )
        genie_imp_res = r_genie_imp.json()["result"]
        assert genie_imp_res["isError"] is False
        genie_sources = {s["source"] for s in genie_imp_res["structuredContent"]["snippets"]}
        assert genie_sources == {"training.gold.onboarding_docs"}
