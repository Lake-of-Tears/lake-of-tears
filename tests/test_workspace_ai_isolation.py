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
    mock_con.execute.side_effect = [mock_probe, mock_search]

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
    _, search_params = mock_con.execute.call_args_list[1].args
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
        patch.object(ad_mod, "provider_info", return_value={"dimension": 4, "embedding_model": "gemini-embedding-001"}),
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
        df = pd.DataFrame([
            {"content": "Metric normal", "embedding": [0.1, 0.1, 0.1, 0.1], "source_date": "2026-10-26"},
            {"content": "Metric outlier", "embedding": [0.9, 0.9, 0.9, 0.9], "source_date": "2026-10-26"},
        ])
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
