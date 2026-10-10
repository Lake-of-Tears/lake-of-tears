from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402


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


def test_ai_query_missing_api_key():
    """Verify _ai_query returns clean error when GEMINI_API_KEY is unset."""
    with patch.object(main, "GEMINI_API_KEY", ""):
        result = main._ai_query("What was revenue in Q1?")
        assert result["ok"] is False
        assert "GEMINI_API_KEY is not set" in result["error"]


def test_ai_vector_sql_executes_against_parquet(tmp_path: Path):
    """Verify AI_VECTOR_SQL executes directly in DuckDB with parameters against a Parquet file."""
    parquet_file = tmp_path / "mock_embeddings.parquet"
    test_vec = [0.1] * 768

    con = duckdb.connect()
    con.execute("INSTALL vss; LOAD vss;")
    con.execute(
        "CREATE TABLE mock_embed (source VARCHAR, ingested_at VARCHAR, summary VARCHAR, embedding DOUBLE[])"
    )
    con.execute(
        "INSERT INTO mock_embed VALUES (?, ?, ?, ?)",
        ["stripe_charges", "2024-01-01 12:00:00", "Total revenue: $5000", test_vec],
    )
    con.execute(f"COPY mock_embed TO '{parquet_file}' (FORMAT PARQUET)")

    # Execute parameterized query
    rows = con.execute(main.AI_VECTOR_SQL, [test_vec, str(parquet_file)]).fetchall()
    con.close()

    assert len(rows) == 1
    source, ingested_at, summary, score = rows[0]
    assert source == "stripe_charges"
    assert summary == "Total revenue: $5000"
    assert pytest.approx(score, rel=1e-4) == 1.0


def test_ai_query_parameterized_execution():
    """Verify _ai_query executes with parameterized statements and expected prompt construction."""
    test_vec = [0.1] * 768

    mock_embed_obj = MagicMock()
    mock_embed_obj.values = test_vec

    mock_embed_resp = MagicMock()
    mock_embed_resp.embeddings = [mock_embed_obj]

    mock_gen_resp = MagicMock()
    mock_gen_resp.text = "According to Stripe charges, total revenue was $5000."

    mock_client = MagicMock()
    mock_client.models.embed_content.return_value = mock_embed_resp
    mock_client.models.generate_content.return_value = mock_gen_resp

    mock_rel = MagicMock()
    mock_rel.fetchall.return_value = [
        ("stripe_charges", "2024-01-01 12:00:00", "Total revenue: $5000", 0.9999)
    ]

    mock_con = MagicMock()
    mock_con.execute.return_value = mock_rel

    with (
        patch.object(main, "GEMINI_API_KEY", "dummy-key"),
        patch("main.genai.Client", return_value=mock_client),
        patch.object(main, "MINIO_BUCKET", "mock-bucket"),
        patch.object(main, "_duckdb_connection", return_value=mock_con) as mock_get_conn,
    ):
        result = main._ai_query("What was total revenue?")

        assert result["ok"] is True
        assert result["answer"] == "According to Stripe charges, total revenue was $5000."
        assert len(result["snippets"]) == 1
        assert result["snippets"][0]["source"] == "stripe_charges"
        assert result["snippets"][0]["score"] == 0.9999

        # Verify _duckdb_connection was called with load_vss=True
        mock_get_conn.assert_called_once_with(load_vss=True)

        # Verify con.execute was called with AI_VECTOR_SQL and [q_vec, s3_pattern]
        mock_con.execute.assert_called_once_with(
            main.AI_VECTOR_SQL,
            [test_vec, "s3://mock-bucket/embeddings/**/*.parquet"],
        )
        mock_con.close.assert_called_once()


def test_ai_query_sql_is_static_constant():
    """Verify AI_VECTOR_SQL does not contain f-string formatting or string interpolations."""
    assert "{" not in main.AI_VECTOR_SQL
    assert "}" not in main.AI_VECTOR_SQL
    assert "?" in main.AI_VECTOR_SQL
    assert "array_cosine_similarity" in main.AI_VECTOR_SQL
