from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

# Ensure repo root and ui directory are in sys.path
repo_root = Path(__file__).parents[1]
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "ui"))

import ui.main as main  # noqa: E402


def test_duckdb_connection_config():
    """Verify that _duckdb_connection applies S3 configuration without SQL SET statements."""
    with (
        patch.object(main, "MINIO_ENDPOINT", "minio.test:9000"),
        patch.object(main, "MINIO_ACCESS_KEY", "test_access_key"),
        patch.object(main, "MINIO_SECRET_KEY", "test_secret_key"),
    ):
        con = main._duckdb_connection(load_vss=False)
        try:
            endpoint = con.execute("SELECT current_setting('s3_endpoint')").fetchone()[0]
            access_key = con.execute("SELECT current_setting('s3_access_key_id')").fetchone()[0]
            use_ssl = con.execute("SELECT current_setting('s3_use_ssl')").fetchone()[0]
            url_style = con.execute("SELECT current_setting('s3_url_style')").fetchone()[0]

            assert endpoint == "minio.test:9000"
            assert access_key == "test_access_key"
            assert use_ssl is False
            assert url_style == "path"
        finally:
            con.close()


def test_duckdb_connection_with_vss():
    """Verify that _duckdb_connection(load_vss=True) successfully loads the vss extension."""
    con = main._duckdb_connection(load_vss=True)
    try:
        # Verify vss function array_cosine_similarity is available
        res = con.execute(
            "SELECT array_cosine_similarity([1.0, 0.0]::DOUBLE[2], [1.0, 0.0]::DOUBLE[2])"
        ).fetchone()[0]
        assert pytest.approx(res, rel=1e-4) == 1.0
    finally:
        con.close()


def test_duckdb_connection_handles_special_characters_in_secret():
    """Verify that passwords containing special characters (quotes, backslashes, semicolons)

    do not cause SQL syntax or injection errors when using config dict.
    """
    special_secret = "p@$$w'or\"d;--DROP TABLE users;"
    with patch.object(main, "MINIO_SECRET_KEY", special_secret):
        con = main._duckdb_connection()
        try:
            secret_setting = con.execute(
                "SELECT current_setting('s3_secret_access_key')"
            ).fetchone()[0]
            assert secret_setting == special_secret
        finally:
            con.close()


def test_run_query_success():
    """Verify _run_query executes valid SQL and returns structured result."""
    result = main._run_query("SELECT 42 AS answer, 'hello' AS greeting")
    assert result["ok"] is True
    assert result["columns"] == ["answer", "greeting"]
    assert result["rows"] == [(42, "hello")]
    assert result["row_count"] == 1


def test_run_query_error():
    """Verify _run_query safely catches and returns errors for invalid SQL."""
    result = main._run_query("SELECT * FROM non_existent_table_12345")
    assert result["ok"] is False
    assert "error" in result
    assert "non_existent_table_12345" in result["error"]


def test_ai_query_without_a_configured_provider():
    """Verify _ai_query returns a clean error when no AI provider is configured."""
    with patch.object(main, "_get_ai_access", return_value=""):
        result = main._ai_query("What was revenue in Q1?")
        assert result["ok"] is False
        assert "No AI provider is configured" in result["error"]


def test_ai_vector_sql_executes_against_parquet(tmp_path: Path):
    """Verify the vector search SQL runs in DuckDB against a Parquet file in the pipeline schema."""
    parquet_file = tmp_path / "mock_embeddings.parquet"
    test_vec = [0.1] * 768

    con = duckdb.connect()
    con.execute("INSTALL vss; LOAD vss;")
    # Same columns that pipeline/embed/embed_sources.py writes to embeddings/<source>/...
    con.execute(
        "CREATE TABLE mock_embed (id VARCHAR, source VARCHAR, content VARCHAR, "
        "embedding DOUBLE[], source_date VARCHAR, ingested_at VARCHAR)"
    )
    con.execute(
        "INSERT INTO mock_embed VALUES (?, ?, ?, ?, ?, ?)",
        [
            "abc123",
            "stripe_charges",
            "Total revenue: $5000",
            test_vec,
            "2024-01-01",
            "2024-01-01 12:00:00",
        ],
    )
    con.execute(f"COPY mock_embed TO '{parquet_file}' (FORMAT PARQUET)")

    # Execute parameterized query
    sql = main.ai_gateway_client.vector_search_sql(768, has_model_column=False)
    rows = con.execute(sql, [test_vec, str(parquet_file), 5]).fetchall()
    con.close()

    assert len(rows) == 1
    source, ingested_at, summary, score = rows[0]
    assert source == "stripe_charges"
    assert summary == "Total revenue: $5000"
    assert pytest.approx(score, rel=1e-4) == 1.0


def test_ai_query_parameterized_execution():
    """Verify _ai_query embeds the question, searches with a dimension-aware parameterized query
    and builds the answer prompt from the matches."""
    test_vec = [0.1] * 4

    mock_embed_resp = MagicMock()
    mock_embed_resp.embeddings = [MagicMock(values=test_vec)]
    mock_embed_resp.model = "stub-embed"
    mock_embed_resp.dimension = 4

    mock_client = MagicMock()
    mock_client.models.embed_content.return_value = mock_embed_resp
    mock_client.models.generate_content.return_value = MagicMock(
        text="According to Stripe charges, total revenue was $5000."
    )

    probe = MagicMock()
    probe.description = [("source",), ("model",), ("embedding",)]  # files record the model
    search = MagicMock()
    search.fetchall.return_value = [
        ("stripe_charges", "2024-01-01 12:00:00", "Total revenue: $5000", 0.9999)
    ]
    mock_con = MagicMock()
    mock_con.execute.side_effect = [probe, search]

    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
        patch.object(main, "MINIO_BUCKET", "mock-bucket"),
        patch.object(main, "_duckdb_connection", return_value=mock_con) as mock_get_conn,
    ):
        result = main._ai_query("What was total revenue?")

    assert result["ok"] is True
    assert result["answer"] == "According to Stripe charges, total revenue was $5000."
    assert result["snippets"][0]["source"] == "stripe_charges"
    assert result["snippets"][0]["score"] == 0.9999

    mock_get_conn.assert_called_once_with()
    s3 = "s3://mock-bucket/workspaces/default/embeddings/**/*.parquet"
    mock_client.models.embed_content.assert_called_once_with(
        contents="What was total revenue?", config={"task_type": "RETRIEVAL_QUERY"}
    )
    # second call is the search: dimension 4, filtered to the embedding model, limit 5
    search_sql, search_params = mock_con.execute.call_args_list[1].args
    assert search_sql == main.ai_gateway_client.vector_search_sql(4, has_model_column=True)
    assert search_params == [test_vec, s3, "stub-embed", 5]
    mock_con.close.assert_called_once()


def test_ai_query_workspace_scoped_and_shared_catalogs():
    """Verify _ai_query scopes search to custom workspace and includes shared catalog vectors."""
    test_vec = [0.2] * 4

    mock_embed_resp = MagicMock()
    mock_embed_resp.embeddings = [MagicMock(values=test_vec)]
    mock_embed_resp.model = "stub-embed"
    mock_embed_resp.dimension = 4

    mock_client = MagicMock()
    mock_client.models.embed_content.return_value = mock_embed_resp
    mock_client.models.generate_content.return_value = MagicMock(text="Workspace response")

    probe = MagicMock()
    probe.description = [("source",), ("model",), ("embedding",)]
    search = MagicMock()
    search.fetchall.return_value = [
        ("custom_source", "2024-01-01 12:00:00", "Custom workspace content", 0.95)
    ]
    mock_con = MagicMock()
    mock_con.execute.side_effect = [probe, search]

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
                                "s3_vector_path": "s3://mock-bucket/workspaces/finance/embeddings/charges/data.parquet"
                            },
                        }
                    ],
                }
            ],
        }
    ]

    with (
        patch.object(main, "_get_ai_access", return_value="gateway"),
        patch.object(main, "_ai_client", return_value=mock_client),
        patch.object(main, "MINIO_BUCKET", "mock-bucket"),
        patch.object(main, "_duckdb_connection", return_value=mock_con),
    ):
        result = main._ai_query(
            "What was revenue in analytics?",
            workspace_slug="analytics",
            catalogs=shared_catalogs,
        )

    assert result["ok"] is True
    search_sql, search_params = mock_con.execute.call_args_list[1].args
    expected_patterns = [
        "s3://mock-bucket/workspaces/analytics/embeddings/**/*.parquet",
        "s3://mock-bucket/workspaces/finance/embeddings/charges/data.parquet",
    ]
    assert search_params == [test_vec, expected_patterns, "stub-embed", 5]


def test_vector_search_sql_only_interpolates_an_integer_dimension():
    """The dimension is the one value formatted into the SQL; everything else is a parameter."""
    sql = main.ai_gateway_client.vector_search_sql(1536, has_model_column=True)
    assert "DOUBLE[1536]" in sql
    assert sql.count("?") == 4  # vector, path, model, limit
    with pytest.raises(ValueError):
        main.ai_gateway_client.vector_search_sql("768; DROP TABLE x", has_model_column=False)


def test_parse_s3_endpoint_and_ssl():
    """Verify parse_s3_endpoint_and_ssl parses schemes, subpaths, and SSL overrides."""
    import duckdb_pool

    # Default host:port without scheme
    ep, ssl = duckdb_pool.parse_s3_endpoint_and_ssl("minio:9000")
    assert ep == "minio:9000"
    assert ssl is False

    # http scheme
    ep, ssl = duckdb_pool.parse_s3_endpoint_and_ssl("http://minio.local:9000")
    assert ep == "minio.local:9000"
    assert ssl is False

    # https scheme
    ep, ssl = duckdb_pool.parse_s3_endpoint_and_ssl("https://s3.us-east-1.amazonaws.com")
    assert ep == "s3.us-east-1.amazonaws.com"
    assert ssl is True

    # explicit secure override
    ep, ssl = duckdb_pool.parse_s3_endpoint_and_ssl("http://minio:9000", secure_flag=True)
    assert ep == "minio:9000"
    assert ssl is True

    # trailing slash/subpath removal
    ep, ssl = duckdb_pool.parse_s3_endpoint_and_ssl("http://minio:9000/datalake")
    assert ep == "minio:9000"
    assert ssl is False


def test_duckdb_connection_pool_lifecycle():
    """Verify DuckDBConnectionPool acquires, reuses, and closes warm connections."""
    import duckdb_pool

    pool = duckdb_pool.DuckDBConnectionPool(max_size=2, load_vss=False)
    assert pool._created_count == 0

    with pool.acquire() as c1:
        res = c1.execute("SELECT 100 AS num").fetchone()[0]
        assert res == 100
        cache_setting = c1.execute("SELECT current_setting('enable_object_cache')").fetchone()[0]
        assert cache_setting is True

    assert pool._created_count == 1

    # Re-acquire: should reuse the pooled connection without increasing count
    with pool.acquire() as c2:
        res2 = c2.execute("SELECT 200 AS num").fetchone()[0]
        assert res2 == 200

    assert pool._created_count == 1
    pool.close_all()


def test_classify_query_error_empty_dataset():
    """Verify empty dataset IOException is classified with extracted storage prefix."""
    import duckdb_pool

    raw_err = Exception(
        'IO Error: No files found that match the pattern "s3://datalake/raw/shopify/**/*.parquet"'
    )
    classified = duckdb_pool.classify_query_error(
        raw_err,
        sql="SELECT * FROM read_parquet('s3://datalake/raw/shopify/**/*.parquet')",
        endpoint="minio:9000",
        bucket="datalake",
    )

    assert classified["error_type"] == "EMPTY_DATASET"
    assert classified["storage_prefix"] == "raw/shopify"
    assert "No files were found matching" in classified["message"]
    assert "Storage Browser" in classified["suggestion"]


def test_classify_query_error_s3_connection():
    """Verify S3 connection failures are identified with host guidance."""
    import duckdb_pool

    raw_err = Exception("IO Error: Could not establish connection to: minio:9000")
    classified = duckdb_pool.classify_query_error(
        raw_err,
        endpoint="minio:9000",
        bucket="datalake",
    )

    assert classified["error_type"] == "S3_CONNECTION"
    assert "Could not access S3 storage at `minio:9000`" in classified["message"]
    assert "MINIO_ACCESS_KEY" in classified["suggestion"]


def test_classify_query_error_syntax():
    """Verify SQL syntax / catalog binder errors recommend AI Fix."""
    import duckdb_pool

    raw_err = Exception('Catalog Error: Table with name "orders" does not exist!')
    classified = duckdb_pool.classify_query_error(
        raw_err,
        sql="SELECT * FROM orders",
    )

    assert classified["error_type"] == "SYNTAX_OR_BINDER"
    assert "Fix with AI" in classified["suggestion"]


def test_run_query_returns_error_info_structure():
    """Verify _run_query returns rich error_info alongside error string on failure."""
    res = main._run_query("SELECT * FROM non_existent_catalog_table_xyz")
    assert res["ok"] is False
    assert "error" in res
    assert "error_info" in res
    assert res["error_info"]["error_type"] == "SYNTAX_OR_BINDER"
    assert "non_existent_catalog_table_xyz" in res["error"]
