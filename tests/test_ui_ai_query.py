from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from starlette.testclient import TestClient

ui_dir = str(Path(__file__).parents[1] / "ui")
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import duckdb_ai  # noqa: E402
import main  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ai_cache():
    duckdb_ai.clear_ai_cache()
    yield
    duckdb_ai.clear_ai_cache()


def _make_mock_genai_client(response_text: str | list[str]):
    mock_client = MagicMock()
    if isinstance(response_text, list):
        resps = []
        for t in response_text:
            r = MagicMock()
            r.text = t
            resps.append(r)
        mock_client.models.generate_content.side_effect = resps
    else:
        mock_resp = MagicMock()
        mock_resp.text = response_text
        mock_client.models.generate_content.return_value = mock_resp
    return mock_client


# ── 1. DuckDB SQL AI Scalar/Vectorized UDF Tests ──────────────────────────────


def test_ai_query_udf_single_and_two_arg_signatures():
    """Verify ai_query supports 1-arg, 2-arg Databricks (model, prompt), and 2-arg (prompt, model)."""
    mock_client = _make_mock_genai_client(["positive", "high_risk", "approved"])
    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        res1 = main._run_query("SELECT ai_query('Classify sentiment: great service') AS s")
        assert res1["ok"] is True
        assert res1["rows"] == [("positive",)]

        res2 = main._run_query(
            "SELECT ai_query('gemini-3.8-flash', 'Assess risk: chargeback') AS risk"
        )
        assert res2["ok"] is True
        assert res2["rows"] == [("high_risk",)]

        res3 = main._run_query(
            "SELECT ai_query('Verify invoice #102', 'gemini-3.8-flash') AS status"
        )
        assert res3["ok"] is True
        assert res3["rows"] == [("approved",)]


def test_ai_udfs_null_handling():
    """Verify NULL inputs to ai_query, ai_summarize, ai_classify, and ai_extract return NULL without calling Gemini."""
    mock_client = _make_mock_genai_client("should-not-be-called")
    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        res = main._run_query(
            """
            SELECT
                ai_query(NULL) AS q_null,
                ai_summarize(NULL) AS s_null,
                ai_classify(NULL, ['a', 'b']) AS c_null,
                ai_extract(NULL, ['f1', 'f2']) AS e_null
            """
        )
        assert res["ok"] is True
        assert res["rows"] == [(None, None, None, None)]
        assert res["ai_calls_made"] == 0
        mock_client.models.generate_content.assert_not_called()


def test_ai_summarize_classify_and_extract_udfs():
    """Verify ai_summarize, ai_classify, and ai_extract execute inside DuckDB SQL and normalize outputs."""
    mock_client = _make_mock_genai_client(
        [
            "Stripe revenue grew 18% month-over-month.",
            "'negotiation'",
            '```json\n{"customer": "Acme Corp", "amount": 1200}\n```',
        ]
    )
    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        s_res = main._run_query(
            "SELECT ai_summarize('Long detailed daily log about Stripe growth...') AS brief"
        )
        assert s_res["ok"] is True
        assert s_res["rows"] == [("Stripe revenue grew 18% month-over-month.",)]

        c_res = main._run_query(
            "SELECT ai_classify('Contract sent to legal team', ['early_stage', 'negotiation', 'closed']) AS stage"
        )
        assert c_res["ok"] is True
        assert c_res["rows"] == [("negotiation",)]

        e_res = main._run_query(
            "SELECT ai_extract('Invoice for Acme Corp total $1200', ['customer', 'amount', 'missing_field']) AS payload"
        )
        assert e_res["ok"] is True
        payload_str = e_res["rows"][0][0]
        parsed = json.loads(payload_str)
        assert parsed == {"customer": "Acme Corp", "amount": 1200, "missing_field": None}


def test_ai_udf_lru_cache_deduplicates_identical_prompts():
    """Verify duplicate prompts within or across queries hit the LRU cache and only call Gemini once."""
    mock_client = _make_mock_genai_client("cached-sentiment")
    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        res = main._run_query(
            """
            SELECT ai_query('Classify: ' || txt) AS label
            FROM (VALUES ('same text'), ('same text'), ('same text')) AS t(txt)
            """
        )
        assert res["ok"] is True
        assert res["rows"] == [
            ("cached-sentiment",),
            ("cached-sentiment",),
            ("cached-sentiment",),
        ]
        assert res["ai_calls_made"] == 1
        assert mock_client.models.generate_content.call_count == 1


def test_ai_udf_row_limit_guardrail():
    """Verify queries exceeding max_ai_rows raise a clear SQL error."""
    mock_client = _make_mock_genai_client("ok")
    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        res = main._run_query(
            "SELECT ai_query('Row ' || i::VARCHAR) FROM range(10) AS t(i)",
            max_ai_rows=3,
        )
        assert res["ok"] is False
        assert "AI function row limit exceeded (3 rows max per query)" in res["error"]


# ── 2. Workspace Catalog View Registration in DuckDB ──────────────────────────


def test_register_catalog_views_queries_parquet_via_schema_table(tmp_path: Path):
    """Verify workspace Catalog tables with s3_path_pattern are registered as schema.table views in DuckDB."""
    parquet_path = tmp_path / "charges.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t (id VARCHAR, amount_usd DOUBLE)")
    con.execute("INSERT INTO t VALUES ('ch_1', 150.5), ('ch_2', 249.5)")
    con.execute(f"COPY t TO '{parquet_path}' (FORMAT PARQUET)")
    con.close()

    catalogs = [
        {
            "name": "default",
            "slug": "default",
            "schemas": [
                {
                    "name": "bronze",
                    "slug": "bronze",
                    "tables": [
                        {
                            "name": "stripe_charges",
                            "slug": "stripe_charges",
                            "s3_path_pattern": str(parquet_path),
                            "column_defs": [
                                {"name": "id", "type": "VARCHAR"},
                                {"name": "amount_usd", "type": "DOUBLE"},
                            ],
                        }
                    ],
                }
            ],
        }
    ]

    result = main._run_query(
        "SELECT COUNT(*), SUM(amount_usd) FROM bronze.stripe_charges",
        catalogs=catalogs,
    )
    assert result["ok"] is True
    assert "bronze.stripe_charges" in result["registered_views"]
    assert result["rows"] == [(2, 400.0)]


# ── 3. UI Endpoints: Text-to-SQL, Fix with AI, Explain Query, and Genie Chat ──


def test_ui_sql_assistant_endpoints():
    """Verify /api/v1/ai/sql/generate, /api/v1/ai/sql/fix, and /api/v1/ai/sql/explain."""
    with (
        patch.object(main, "AUTH_ENABLED", False),
        patch.object(main, "_get_ai_access", return_value="gateway"),
    ):
        client = TestClient(main.app)

        # 1. Generate SQL
        gen_mock = _make_mock_genai_client(
            json.dumps(
                {
                    "sql": "SELECT date_trunc('month', created_at) AS month, SUM(amount_usd) AS rev FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet') GROUP BY 1",
                    "explanation": "Aggregates monthly revenue from Stripe charges.",
                    "tables_used": ["stripe_charges"],
                    "ai_functions_used": [],
                }
            )
        )
        with patch.object(main, "_ai_client", return_value=gen_mock):
            resp = client.post(
                "/api/v1/ai/sql/generate",
                json={"prompt": "Show monthly revenue from Stripe charges"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert "stripe_charges" in data["sql"]
            assert data["tables_used"] == ["stripe_charges"]

        # 2. Fix SQL
        fix_mock = _make_mock_genai_client(
            json.dumps(
                {
                    "fixed_sql": "SELECT amount_usd FROM read_parquet('s3://datalake/raw/stripe_charges/**/*.parquet') LIMIT 10",
                    "explanation": "Replaced non-existent column `amount` with `amount_usd`.",
                }
            )
        )
        with patch.object(main, "_ai_client", return_value=fix_mock):
            resp = client.post(
                "/api/v1/ai/sql/fix",
                json={
                    "sql": "SELECT amount FROM stripe_charges",
                    "error": "Binder Error: Referenced column 'amount' not found",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert "amount_usd" in data["fixed_sql"]

        # 3. Explain SQL
        exp_mock = _make_mock_genai_client(
            json.dumps(
                {
                    "summary": "Computes top products by revenue.",
                    "steps": ["Reads shopify_line_items parquet files", "Groups by title"],
                    "tables_referenced": ["shopify_line_items"],
                    "ai_functions_used": [],
                }
            )
        )
        with patch.object(main, "_ai_client", return_value=exp_mock):
            resp = client.post(
                "/api/v1/ai/sql/explain",
                json={"sql": "SELECT title, SUM(price_usd) FROM shopify GROUP BY 1"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["summary"] == "Computes top products by revenue."
            assert len(data["steps"]) == 2


def test_ui_genie_chat_self_correction_and_chart_inference():
    """Verify /api/v1/ai/chat synthesizes SQL, self-corrects on DuckDB error, executes query, and returns chart config."""
    mock_client = _make_mock_genai_client(
        [
            # Step 1: Initial SQL synthesis (has a deliberate broken table name to trigger self-correction)
            json.dumps(
                {
                    "sql": "SELECT * FROM broken_nonexistent_table_xyz",
                    "tables_used": ["stripe_charges"],
                    "reasoning": "Initial attempt",
                }
            ),
            # Step 2: 1-step self-correction fix response
            json.dumps(
                {
                    "fixed_sql": "SELECT '2026-01' AS month, 12500.0 AS revenue UNION ALL SELECT '2026-02' AS month, 15800.0 AS revenue",
                    "explanation": "Fixed query to return monthly revenue rows.",
                }
            ),
            # Step 3: Grounded narrative synthesis
            "Monthly revenue increased from $12,500 in January to $15,800 in February (+26.4%).",
        ]
    )

    with (
        patch.object(main, "AUTH_ENABLED", False),
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        client = TestClient(main.app)
        resp = client.post(
            "/api/v1/ai/chat",
            json={
                "question": "What was our monthly revenue trend?",
                "mode": "genie",
                "history": [],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["self_corrected"] is True
        assert data["row_count"] == 2
        assert data["columns"] == ["month", "revenue"]
        assert data["chart_config"]["type"] == "line"
        assert "$15,800" in data["answer"]


def test_ui_genie_chat_stream_events_and_result():
    """Verify /api/v1/ai/chat/stream emits step SSE events and final result payload."""
    mock_client = _make_mock_genai_client(
        [
            # Step 1: Initial SQL synthesis (triggers self-correction)
            json.dumps(
                {
                    "sql": "SELECT * FROM broken_table_for_stream",
                    "tables_used": ["stripe_charges"],
                    "reasoning": "Attempt",
                }
            ),
            # Step 2: Fix response
            json.dumps(
                {
                    "fixed_sql": "SELECT '2026-01' AS month, 5000.0 AS rev",
                    "explanation": "Fixed SQL.",
                }
            ),
            # Step 3: Grounded narrative
            "January revenue was $5,000.",
        ]
    )

    with (
        patch.object(main, "AUTH_ENABLED", False),
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
    ):
        client = TestClient(main.app)
        resp = client.post(
            "/api/v1/ai/chat/stream",
            json={
                "question": "What was January revenue?",
                "mode": "genie",
                "history": [],
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text
        assert "event: step" in text
        assert "event: result" in text
        assert "schema" in text
        assert "sql_gen" in text
        assert "sql_exec" in text
        assert "sql_fix" in text
        assert "insight" in text
        assert "January revenue was $5,000." in text


def test_ui_genie_chat_stream_rag_mode():
    """Verify /api/v1/ai/chat/stream emits step events in RAG mode."""
    with (
        patch.object(main, "AUTH_ENABLED", False),
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(
            main,
            "_ai_query",
            return_value={
                "ok": True,
                "answer": "RAG search answer",
                "snippets": [{"source": "s3/doc", "score": 0.95, "summary": "Snippet"}],
            },
        ),
    ):
        client = TestClient(main.app)
        resp = client.post(
            "/api/v1/ai/chat/stream",
            json={
                "question": "Explain documentation",
                "mode": "rag",
                "history": [],
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text
        assert "event: step" in text
        assert "rag_search" in text
        assert "event: result" in text
        assert "RAG search answer" in text


def test_ui_asks_the_backend_gateway_whether_an_ai_provider_is_configured():
    """The UI holds no AI credentials: availability and the chat model come from the gateway."""
    info = {"provider": "stub", "chat_model": "stub-chat", "embedding_model": "e", "dimension": 4}
    with patch.object(main.ai_gateway_client, "provider_info", return_value=info):
        assert main._get_ai_access() == "gateway"
        assert main._get_gemini_default_model() == "stub-chat"
    with patch.object(main.ai_gateway_client, "provider_info", return_value=None):
        assert main._get_ai_access() == ""
        assert main._get_gemini_default_model() == duckdb_ai.DEFAULT_GEMINI_MODEL


def _superadmin_client(role: str = "superadmin") -> tuple[TestClient, str]:
    import jwt

    token = jwt.encode(
        {"sub": "admin-1", "email": "admin@lake.local", "role": role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    return TestClient(main.app, cookies={"lake_token": token}), token


_WS = [{"id": "ws-1", "slug": "default", "name": "Default"}]


def test_admin_panel_saves_the_selected_providers_settings_through_the_backend():
    client, token = _superadmin_client()
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch.object(main, "_backend_put", return_value=(200, {})) as put,
    ):
        resp = client.post(
            "/settings/admin/ai-provider",
            data={
                "provider": "gemini",
                "cfg__gemini__api_key": "AIzaSyNewKey1234",
                "cfg__gemini__chat_model": "m1",
                "cfg__other__api_key": "ignored",  # another provider's field is not sent
            },
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/admin?saved=1#ai-settings"
    put.assert_called_once_with(
        "/api/admin/ai-provider",
        token,
        {
            "provider": "gemini",
            "config": {"api_key": "AIzaSyNewKey1234", "chat_model": "m1"},
            "activate": True,
        },
    )


def test_admin_panel_clears_one_secret_without_changing_the_active_provider():
    client, token = _superadmin_client()
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch.object(main, "_backend_put", return_value=(200, {})) as put,
    ):
        client.post(
            "/settings/admin/ai-provider",
            data={"provider": "gemini", "action": "clear-secret", "secret_field": "api_key"},
            follow_redirects=False,
        )
    put.assert_called_once_with(
        "/api/admin/ai-provider",
        token,
        {"provider": "gemini", "config": {"api_key": None}, "activate": False},
    )


def test_admin_panel_shows_a_backend_validation_error():
    client, _ = _superadmin_client()
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch.object(
            main, "_backend_put", return_value=(400, {"detail": "Dimension must be a whole number"})
        ),
    ):
        resp = client.post(
            "/settings/admin/ai-provider",
            data={"provider": "gemini", "cfg__gemini__dimension": "abc"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "ai_error=Dimension%20must%20be%20a%20whole%20number" in resp.headers["location"]


def test_admin_provider_routes_are_superadmin_only():
    client, _ = _superadmin_client(role="user")
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch.object(main, "_backend_put") as put,
    ):
        resp = client.post(
            "/settings/admin/ai-provider", data={"provider": "gemini"}, follow_redirects=False
        )
        test_resp = client.post("/settings/admin/ai-provider/test", json={"provider": "gemini"})
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    assert test_resp.status_code == 403
    put.assert_not_called()


def test_admin_provider_test_route_passes_unsaved_settings_to_the_backend():
    client, token = _superadmin_client()
    reply = MagicMock(status_code=200)
    reply.json.return_value = {"ok": True, "message": "Connected", "chat": {"latency_ms": 5}}
    http = MagicMock()
    http.__enter__.return_value.post.return_value = reply
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch("main.httpx.Client", return_value=http),
    ):
        resp = client.post(
            "/settings/admin/ai-provider/test",
            json={"provider": "gemini", "config": {"chat_model": "trial"}},
        )
    assert resp.json()["ok"] is True
    call = http.__enter__.return_value.post.call_args
    assert call.args[0].endswith("/api/admin/ai-provider/test")
    assert call.kwargs["json"] == {"provider": "gemini", "config": {"chat_model": "trial"}}
    assert call.kwargs["headers"] == {"Authorization": f"Bearer {token}"}


_DESCRIBE = {
    "active": "gemini",
    "providers": [
        {
            "name": "gemini",
            "label": "Google Gemini",
            "configured": True,
            "fields": [
                {
                    "name": "api_key",
                    "label": "API key",
                    "kind": "secret",
                    "required": True,
                    "default": None,
                    "help": "From AI Studio.",
                    "placeholder": "",
                },
                {
                    "name": "chat_model",
                    "label": "Chat model",
                    "kind": "text",
                    "required": False,
                    "default": "gemini-3.8-flash",
                    "help": "",
                    "placeholder": "",
                },
                {
                    "name": "dimension",
                    "label": "Embedding dimension",
                    "kind": "int",
                    "required": False,
                    "default": 768,
                    "help": "",
                    "placeholder": "",
                },
            ],
            "config": {
                "provider": "gemini",
                "api_key_configured": True,
                "api_key_masked": "\u2022\u2022\u2022\u20221234",
                "chat_model": "my-model",
                "dimension": 768,
            },
        },
        {
            "name": "other",
            "label": "Other Provider",
            "configured": False,
            "fields": [
                {
                    "name": "base_url",
                    "label": "Base URL",
                    "kind": "text",
                    "required": True,
                    "default": None,
                    "help": "",
                    "placeholder": "http://host:11434",
                },
            ],
            "config": {"provider": "other"},
        },
    ],
}


def _backend_get_for_admin(path, token):
    if path == "/api/admin/ai-provider":
        return _DESCRIBE
    return None


def test_admin_page_renders_every_provider_from_the_backend_schema():
    client, _ = _superadmin_client()
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch.object(main, "_backend_get", side_effect=_backend_get_for_admin),
    ):
        html = client.get("/settings/admin?ai_error=Bad%20value").text
    assert 'id="ai-provider-select"' in html
    assert "Google Gemini" in html and "Other Provider" in html
    assert 'name="cfg__gemini__api_key"' in html and 'type="password"' in html
    assert 'name="cfg__gemini__chat_model"' in html and 'value="my-model"' in html
    assert 'name="cfg__gemini__dimension"' in html and 'type="number"' in html
    assert 'name="cfg__other__base_url"' in html
    assert "\u2022\u2022\u2022\u20221234" in html  # masked secret only
    assert "Test Connection" in html and "Save and activate" in html
    assert "Bad value" in html  # error banner
    # the inactive provider's fields are hidden and disabled so they are not submitted
    other = html[
        html.index('data-provider="other"') - 120 : html.index('data-provider="other"') + 160
    ]
    assert "hidden" in other and "disabled" in other


def test_admin_page_survives_an_unavailable_backend():
    client, _ = _superadmin_client()
    with (
        patch.object(main, "_fetch_workspaces", return_value=_WS),
        patch.object(main, "_backend_get", return_value=None),
    ):
        resp = client.get("/settings/admin")
    assert resp.status_code == 200
    assert "could not be loaded" in resp.text
