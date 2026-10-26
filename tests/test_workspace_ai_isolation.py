from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import ai_service
import duckdb_ai
import vector_index_service
from sql_guardrails import (
    extract_s3_patterns_from_catalogs,
    is_s3_path_allowed,
    validate_sql_s3_paths,
)
from starlette.testclient import TestClient

import backend.main as backend_main

DIMS = 4


def _dummy_vec(val: float = 0.5) -> list[float]:
    return [val] * DIMS


class FakeAIProvider:
    name = "fake"
    embedding_model = "fake-embed"
    dimension = DIMS

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [_dummy_vec(0.1 * (i + 1)) for i in range(len(texts))]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Table Vector Indexing Workspace Prefix
# ─────────────────────────────────────────────────────────────────────────────


def test_vector_index_sync_writes_to_workspace_prefix(tmp_path: Path):
    """Ensure sync_table_vector_index writes to workspaces/<slug>/embeddings/<tbl>/data.parquet."""
    mock_table = MagicMock()
    mock_table.slug = "customer_churn"
    mock_table.s3_path_pattern = str(tmp_path / "raw.parquet")
    mock_table.vector_index = {"source_column": "text"}

    # Create dummy source parquet file
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE raw (id VARCHAR, text VARCHAR)")
    con.execute("INSERT INTO raw VALUES ('c1', 'Churn risk high'), ('c2', 'Loyal customer')")
    con.execute(f"COPY raw TO '{mock_table.s3_path_pattern}' (FORMAT PARQUET)")
    con.close()

    mock_db = MagicMock()
    provider = FakeAIProvider()

    with (
        patch("vector_index_service.get_active_provider", return_value=provider),
        patch("vector_index_service.create_duckdb_connection") as mock_conn_factory,
    ):
        mock_con = MagicMock()
        mock_conn_factory.return_value = mock_con
        mock_con.execute.return_value.fetchall.side_effect = [
            [("text", "VARCHAR"), ("id", "VARCHAR")],  # DESCRIBE
            [("c1", "Churn risk high"), ("c2", "Loyal customer")],  # SELECT records
        ]

        res = vector_index_service.sync_table_vector_index(
            mock_table,
            mock_db,
            workspace_slug="finance",
            provider=provider,
        )

    assert res["ok"] is True
    expected_path = "s3://datalake/workspaces/finance/embeddings/customer_churn/data.parquet"
    assert res["s3_vector_path"] == expected_path
    assert mock_table.vector_index["s3_vector_path"] == expected_path


# ─────────────────────────────────────────────────────────────────────────────
# 2. SQL Guardrails Isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_guardrails_reject_unshared_root_and_cross_workspace_embeddings():
    """Verify deprecated embeddings/ prefix and unauthorized cross-workspace embeddings are rejected."""
    # 1. Global embeddings/ is blocked
    allowed_root, err_root = is_s3_path_allowed(
        "s3://datalake/embeddings/marketing/data.parquet",
        workspace_slug="analytics",
    )
    assert allowed_root is False
    assert "outside permitted prefix" in err_root

    # 2. Cross-workspace embeddings are blocked without shared catalog
    allowed_other, err_other = is_s3_path_allowed(
        "s3://datalake/workspaces/marketing/embeddings/churn/data.parquet",
        workspace_slug="analytics",
    )
    assert allowed_other is False
    assert "outside permitted prefix" in err_other

    # 3. Active workspace embeddings are allowed
    allowed_mine, _ = is_s3_path_allowed(
        "s3://datalake/workspaces/analytics/embeddings/churn/data.parquet",
        workspace_slug="analytics",
    )
    assert allowed_mine is True


def test_guardrails_allow_shared_catalog_s3_vector_path():
    """Verify shared catalog s3_vector_path is extracted and permitted by guardrails."""
    shared_catalogs = [
        {
            "slug": "shared_cat",
            "schemas": [
                {
                    "slug": "gold",
                    "tables": [
                        {
                            "slug": "shared_churn",
                            "s3_path_pattern": "s3://datalake/workspaces/marketing/raw/churn/**/*.parquet",
                            "vector_index": {
                                "s3_vector_path": "s3://datalake/workspaces/marketing/embeddings/churn/data.parquet"
                            },
                        }
                    ],
                }
            ],
        }
    ]

    extracted = extract_s3_patterns_from_catalogs(shared_catalogs)
    assert "s3://datalake/workspaces/marketing/embeddings/churn/data.parquet" in extracted

    # Query with SQL guardrail validation
    sql = "SELECT * FROM read_parquet('s3://datalake/workspaces/marketing/embeddings/churn/data.parquet')"
    valid, err = validate_sql_s3_paths(
        sql=sql,
        workspace_slug="analytics",
        catalogs=shared_catalogs,
    )
    assert valid is True
    assert err is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. RAG Search Isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_rag_search_scopes_to_workspace_and_shared_catalogs():
    """Verify run_rag_search executes against active workspace prefix and shared vector paths."""
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.embeddings = [MagicMock(values=_dummy_vec(0.1))]
    mock_resp.model = "gemini-embedding-001"
    mock_resp.dimension = DIMS
    mock_client.models.embed_content.return_value = mock_resp
    mock_client.models.generate_content.return_value.text = "Synthesized answer"

    mock_con = MagicMock()
    mock_probe = MagicMock()
    mock_probe.description = [("source",), ("model",), ("embedding",)]
    mock_search = MagicMock()
    mock_search.fetchall.return_value = [
        ("analytics_churn", "2026-10-26", "High churn probability", 0.98)
    ]
    mock_con.execute.side_effect = [MagicMock(), MagicMock(), mock_probe, mock_search]

    shared_catalogs = [
        {
            "slug": "finance_cat",
            "schemas": [
                {
                    "slug": "gold",
                    "tables": [
                        {
                            "slug": "charges",
                            "vector_index": {
                                "s3_vector_path": "s3://datalake/workspaces/finance/embeddings/charges/data.parquet"
                            },
                        }
                    ],
                }
            ],
        }
    ]

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="fake_key"),
        patch.object(ai_service, "_client_for", return_value=mock_client),
        patch.object(ai_service, "create_duckdb_connection", return_value=mock_con),
    ):
        res = ai_service.run_rag_search(
            question="Analyze customer churn",
            workspace_slug="analytics",
            catalogs=shared_catalogs,
        )

    assert res["ok"] is True
    assert res["answer"] == "Synthesized answer"
    assert res["snippets"][0]["source"] == "analytics_churn"

    # Verify query parameters passed to DuckDB vector search
    _, search_params = mock_con.execute.call_args_list[-1].args
    expected_patterns = [
        "s3://datalake/workspaces/analytics/embeddings/**/*.parquet",
        "s3://datalake/workspaces/finance/embeddings/charges/data.parquet",
    ]
    assert search_params[1] == expected_patterns


# ─────────────────────────────────────────────────────────────────────────────
# 4. Genie Schema Prompt Workspace Pure Context
# ─────────────────────────────────────────────────────────────────────────────


def test_schema_prompt_does_not_contain_legacy_root_datasets():
    """Verify build_schema_prompt_context does not inject DEFAULT_LAKEHOUSE_DATASETS pointing to root S3."""
    context_empty = ai_service.build_schema_prompt_context([])
    assert "### Default Lakehouse S3 Parquet Datasets" not in context_empty
    assert "s3://datalake/raw/stripe_charges" not in context_empty
    assert "s3://datalake/embeddings/**/*.parquet" not in context_empty
    assert "No custom tables registered in this workspace catalog yet" in context_empty

    # Same check for UI duckdb_ai
    ui_context_empty = duckdb_ai.build_schema_prompt_context([])
    assert "### Default Lakehouse S3 Parquet Datasets" not in ui_context_empty
    assert "s3://datalake/raw/stripe_charges" not in ui_context_empty
    assert "s3://datalake/embeddings/**/*.parquet" not in ui_context_empty
    assert "No custom tables registered in this workspace catalog yet" in ui_context_empty


# ─────────────────────────────────────────────────────────────────────────────
# 5. Workspace-Scoped Anomaly Detection
# ─────────────────────────────────────────────────────────────────────────────


def test_detect_anomalies_scopes_to_workspace_embeddings():
    """Verify detect_anomalies executes against workspaces/<slug>/embeddings/."""
    import pipeline.embed.anomaly_detect as ad_mod

    mock_clf = MagicMock()
    mock_clf.fit_predict.return_value = [-1, 1]

    with (
        patch.object(
            ad_mod,
            "provider_info",
            return_value={"dimension": 4, "embedding_model": "gemini-embedding-001"},
        ),
        patch.object(ad_mod, "IsolationForest", return_value=mock_clf),
        patch("duckdb.connect") as mock_duckdb_con,
        patch.object(ad_mod, "generate", return_value="Summary of anomalies"),
    ):
        con_instance = MagicMock()
        mock_duckdb_con.return_value = con_instance

        # Probe description
        mock_probe = MagicMock()
        mock_probe.description = [("content",), ("embedding",), ("source_date",), ("model",)]

        # DataFrame simulation
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "content": "Metric normal",
                    "embedding": [0.1, 0.1, 0.1, 0.1],
                    "source_date": "2026-10-26",
                },
                {
                    "content": "Metric outlier",
                    "embedding": [0.9, 0.9, 0.9, 0.9],
                    "source_date": "2026-10-26",
                },
            ]
        )
        mock_probe.df.return_value = df
        con_instance.execute.return_value = mock_probe

        summary = ad_mod.detect_anomalies(workspace_slug="analytics", source="truenas")

    assert "Summary of anomalies" in summary
    calls = [str(c) for c in con_instance.execute.call_args_list]
    matched = any("workspaces/analytics/embeddings/truenas/**/*.parquet" in c for c in calls)
    assert matched, f"Expected workspace glob in execute calls: {calls}"


def test_detect_workspace_anomalies_endpoint():
    """Verify POST /api/workspaces/{workspace_id}/anomalies/detect executes per-workspace."""
    import uuid

    import auth
    import database

    client = TestClient(backend_main.app)
    ws_id = str(uuid.uuid4())
    mock_user = MagicMock()
    mock_user.id = str(uuid.uuid4())
    mock_user.role = "member"

    mock_ws = MagicMock()
    mock_ws.id = ws_id
    mock_ws.slug = "marketing"
    mock_ws.status = "active"

    mock_membership = MagicMock()
    mock_membership.workspace_id = ws_id
    mock_user.workspace_memberships = [mock_membership]

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_ws

    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: mock_user
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    mock_detect = MagicMock(return_value="Marketing anomalies summary")
    fake_mod = MagicMock(detect_anomalies=mock_detect)

    try:
        with patch.dict("sys.modules", {"anomaly_detect": fake_mod}):
            resp = client.post(
                f"/api/workspaces/{ws_id}/anomalies/detect",
                json={"source": "stripe", "contamination": 0.05},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["workspace"] == "marketing"
        assert data["summary"] == "Marketing anomalies summary"
        mock_detect.assert_called_once_with(
            source="stripe",
            contamination=0.05,
            workspace_slug="marketing",
            table_slug=None,
        )
    finally:
        backend_main.app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Backend AI Router Workspace Scoping & Streaming Isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_backend_genie_chat_passes_workspace_slug():
    """Verify backend POST /api/v1/ai/chat strictly routes query to active workspace."""
    import ai_router
    import auth
    import database

    client = TestClient(backend_main.app)
    mock_ws = MagicMock()
    mock_ws.id = "3fe09c2f-f592-4f1d-968b-a662cb2172f1"
    mock_ws.slug = "prd"

    mock_user = MagicMock()
    mock_user.role = "member"

    mock_db = MagicMock()

    backend_main.app.dependency_overrides[ai_router._resolve_ai_context] = lambda: (
        mock_ws,
        "editor",
        mock_user,
    )
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: mock_user
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    with (
        patch("ai_service.get_workspace_catalogs_from_db", return_value=[]),
        patch(
            "ai_service.run_genie_chat", return_value={"ok": True, "mode": "genie", "answer": "OK"}
        ) as mock_run,
    ):
        resp = client.post(
            "/api/v1/ai/chat",
            json={
                "question": "Tell me about research",
                "mode": "rag",
                "history": [],
            },
        )

    assert resp.status_code == 200
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["workspace_slug"] == "prd"
    assert mock_run.call_args.kwargs["is_superadmin"] is False
    backend_main.app.dependency_overrides.clear()


def test_backend_genie_chat_stream_passes_workspace_slug_and_emits_sse():
    """Verify backend POST /api/v1/ai/chat/stream emits SSE events and scopes to active workspace."""
    import ai_router
    import auth
    import database

    client = TestClient(backend_main.app)
    mock_ws = MagicMock()
    mock_ws.id = "3fe09c2f-f592-4f1d-968b-a662cb2172f1"
    mock_ws.slug = "prd"

    mock_user = MagicMock()
    mock_user.role = "member"

    mock_db = MagicMock()

    backend_main.app.dependency_overrides[ai_router._resolve_ai_context] = lambda: (
        mock_ws,
        "editor",
        mock_user,
    )
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: mock_user
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    def fake_run_genie(**kwargs):
        cb = kwargs.get("progress_callback")
        if cb:
            cb({"stage": "rag_search", "label": "Retrieving embeddings", "status": "running"})
        return {"ok": True, "mode": "rag", "answer": "PRD isolated answer", "snippets": []}

    with (
        patch("ai_service.get_workspace_catalogs_from_db", return_value=[]),
        patch("ai_service.run_genie_chat", side_effect=fake_run_genie) as mock_run,
    ):
        resp = client.post(
            "/api/v1/ai/chat/stream",
            json={
                "question": "Tell me about research",
                "mode": "rag",
                "history": [],
            },
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: step" in resp.text
    assert "event: result" in resp.text
    assert "PRD isolated answer" in resp.text
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["workspace_slug"] == "prd"
    backend_main.app.dependency_overrides.clear()


def test_run_rag_search_isolated_from_other_workspaces():
    """Verify run_rag_search on 'prd' workspace does NOT read 'default' workspace embeddings."""
    mock_client = MagicMock()
    embed_obj = MagicMock()
    embed_obj.values = [0.1] * 768
    mock_resp = MagicMock()
    mock_resp.embeddings = [embed_obj]
    mock_resp.model = "test-model"
    mock_resp.dimension = 768
    mock_client.models.embed_content.return_value = mock_resp
    mock_client.models.generate_content.return_value.text = "No embeddings found for PRD."

    with (
        patch("ai_service._resolve_api_key", return_value="dummy_key"),
        patch("ai_service._client_for", return_value=mock_client),
        patch("ai_service.create_duckdb_connection") as mock_conn_factory,
    ):
        mock_con = MagicMock()
        mock_conn_factory.return_value = mock_con
        # DuckDB glob fails when no files exist in prd
        mock_con.execute.side_effect = Exception("No files found that match the pattern")

        result = ai_service.run_rag_search(
            question="Tell me about t-cell research",
            workspace_slug="prd",
            catalogs=[],
        )

    assert result["ok"] is True
    assert result["snippets"] == []
    assert "No embeddings found for PRD." in result["answer"]
    # Check that DuckDB was queried with the prd prefix, NOT default
    call_args = mock_con.execute.call_args_list[0]
    sql_pattern = call_args[0][1][0]
    assert "workspaces/prd/embeddings/**/*.parquet" in sql_pattern
    assert "default" not in sql_pattern


def test_get_workspace_catalogs_from_db_includes_vector_index_and_workspace_shares():
    """Verify get_workspace_catalogs_from_db includes vector_index and approved WorkspaceShares."""
    mock_db = MagicMock()
    mock_table = MagicMock()
    mock_table.id = "t1"
    mock_table.slug = "clinical_trials"
    mock_table.name = "Clinical Trials"
    mock_table.description = "Medical research data"
    mock_table.schema_definition = []
    mock_table.s3_path_pattern = "s3://datalake/workspaces/default/clinical_trials.parquet"
    mock_table.vector_index = {
        "s3_vector_path": "s3://datalake/workspaces/default/embeddings/clinical_trials/data.parquet",
        "source_column": "description",
    }

    mock_schema = MagicMock()
    mock_schema.id = "s1"
    mock_schema.slug = "bronze"
    mock_schema.name = "Bronze"
    mock_schema.tables = [mock_table]

    mock_catalog = MagicMock()
    mock_catalog.id = "c1"
    mock_catalog.slug = "default"
    mock_catalog.name = "Default Catalog"
    mock_catalog.workspace_id = "ws-default"
    mock_catalog.schemas = [mock_schema]

    def mock_db_query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value.all.return_value = [mock_catalog]
        q.all.return_value = [mock_catalog]
        return q

    mock_db.query.side_effect = mock_db_query

    catalogs = ai_service.get_workspace_catalogs_from_db(
        db=mock_db,
        workspace_id="ws-dev",
        user_id="user-123",
    )

    assert len(catalogs) == 1
    assert catalogs[0]["slug"] == "default"
    tbl_result = catalogs[0]["schemas"][0]["tables"][0]
    assert tbl_result["slug"] == "clinical_trials"
    assert "vector_index" in tbl_result
    assert (
        tbl_result["vector_index"]["s3_vector_path"]
        == "s3://datalake/workspaces/default/embeddings/clinical_trials/data.parquet"
    )


def test_run_rag_search_with_shared_catalog_when_active_workspace_empty():
    """Verify run_rag_search ignores empty active workspace embeddings and queries shared catalog."""
    mock_client = MagicMock()
    embed_obj = MagicMock()
    embed_obj.values = [0.2] * 768
    mock_resp = MagicMock()
    mock_resp.embeddings = [embed_obj]
    mock_resp.model = "text-embedding-004"
    mock_resp.dimension = 768
    mock_client.models.embed_content.return_value = mock_resp
    mock_client.models.generate_content.return_value.text = "Answer from clinical_trials"

    mock_con = MagicMock()
    # Pattern 1 (dev workspace) throws No files found
    # Pattern 2 (shared clinical_trials) succeeds
    # Probe on target_pattern succeeds
    mock_probe = MagicMock()
    mock_probe.description = [("source",), ("model",), ("embedding",)]
    mock_search = MagicMock()
    mock_search.fetchall.return_value = [
        ("clinical_trials", "2026-10-26", "T-Cell research active in Phase 3", 0.95)
    ]

    def mock_execute_side_effect(sql, params=None):
        if params and "workspaces/dev/embeddings" in str(params):
            raise Exception(
                "No files found that match the pattern workspaces/dev/embeddings/**/*.parquet"
            )
        if "LIMIT 0" in sql and params and "clinical_trials" in str(params):
            return MagicMock()
        if "SELECT * FROM read_parquet" in sql:
            return mock_probe
        return mock_search

    mock_con.execute.side_effect = mock_execute_side_effect

    catalogs = [
        {
            "slug": "default",
            "schemas": [
                {
                    "slug": "bronze",
                    "tables": [
                        {
                            "slug": "clinical_trials",
                            "vector_index": {
                                "s3_vector_path": "s3://datalake/workspaces/default/embeddings/clinical_trials/data.parquet"
                            },
                        }
                    ],
                }
            ],
        }
    ]

    with (
        patch.object(ai_service, "_resolve_api_key", return_value="fake_key"),
        patch.object(ai_service, "_client_for", return_value=mock_client),
        patch.object(ai_service, "create_duckdb_connection", return_value=mock_con),
    ):
        result = ai_service.run_rag_search(
            question="Tell me about all the t-cell reasearch",
            workspace_slug="dev",
            catalogs=catalogs,
        )

    assert result["ok"] is True
    assert result["answer"] == "Answer from clinical_trials"
    assert len(result["snippets"]) == 1
    assert result["snippets"][0]["source"] == "clinical_trials"


def test_resolve_ui_all_catalogs_merges_owned_and_shared():
    """Verify _resolve_ui_all_catalogs combines owned and shared catalogs without duplicates."""
    import ui.main as ui_main

    mock_req = MagicMock()
    owned_cat = {"id": "cat-1", "slug": "dev_cat", "name": "Dev Catalog"}
    shared_cat_1 = {"id": "cat-1", "slug": "dev_cat", "name": "Dev Catalog Dup"}
    shared_cat_2 = {"id": "cat-2", "slug": "default", "name": "Default Shared"}

    mock_shared_group = {
        "workspace_id": "ws-default",
        "catalogs": [shared_cat_1, shared_cat_2],
    }

    with (
        patch.object(ui_main, "_resolve_ui_catalogs", return_value=[owned_cat]),
        patch.object(ui_main, "_resolve_ui_shared_catalogs", return_value=[mock_shared_group]),
    ):
        all_cats = ui_main._resolve_ui_all_catalogs(mock_req)

    assert len(all_cats) == 2
    ids = [c["id"] for c in all_cats]
    assert ids == ["cat-1", "cat-2"]
