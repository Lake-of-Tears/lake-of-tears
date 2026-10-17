from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import ai_service
import duckdb
import duckdb_ai
import main
import pytest

DIMS = 768


def _vec(seed: int) -> list[float]:
    raw = [math.sin(seed * 12.9898 + i * 0.7) for i in range(DIMS)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


@pytest.fixture
def embeddings_file(tmp_path: Path) -> str:
    """A Parquet file with exactly the columns pipeline/embed/embed_sources.py writes."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (id VARCHAR, source VARCHAR, content VARCHAR, embedding DOUBLE[], "
        "source_date VARCHAR, ingested_at TIMESTAMP)"
    )
    for i, text in enumerate(["Pool tank: ONLINE", "Weather 10:00: 55F", "Movie night"]):
        con.execute(
            "INSERT INTO t VALUES (?, 'demo', ?, ?, '2026-10-16', now())", [f"id{i}", text, _vec(i)]
        )
    path = str(tmp_path / "data.parquet")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    return path


@pytest.mark.parametrize("module", [main, ai_service], ids=["ui", "backend"])
def test_vector_sql_runs_against_pipeline_embeddings_schema(module, embeddings_file):
    rows = duckdb.connect().execute(module.AI_VECTOR_SQL, [_vec(1), embeddings_file]).fetchall()
    assert len(rows) == 3
    source, _ingested_at, text, score = rows[0]
    assert (source, text) == ("demo", "Weather 10:00: 55F")  # nearest to _vec(1)
    assert score == pytest.approx(1.0)


def _declared_embedding_columns(datasets: list[dict]) -> list[str]:
    tables = [d for d in datasets if d["name"] == "embeddings"]
    assert len(tables) == 1, "expected exactly one declared 'embeddings' table"
    return [c["name"] for c in tables[0]["columns"]]


@pytest.mark.parametrize(
    "datasets",
    [duckdb_ai.get_default_datasets(), ai_service.DEFAULT_LAKEHOUSE_DATASETS],
    ids=["ui", "backend"],
)
def test_declared_embeddings_schema_matches_the_pipeline_output(datasets):
    # Used as table hints by the SQL assistant, so it must list the columns that really exist.
    assert _declared_embedding_columns(datasets) == [
        "id",
        "source",
        "content",
        "embedding",
        "source_date",
        "ingested_at",
    ]


def _stub_client() -> MagicMock:
    client = MagicMock()
    client.models.embed_content.return_value.embeddings = [MagicMock(values=_vec(0))]
    client.models.generate_content.return_value.text = "answer"
    return client


def _assert_question_is_embedded_like_the_pipeline(client: MagicMock):
    kwargs = client.models.embed_content.call_args.kwargs
    assert kwargs["model"] == "gemini-embedding-001"
    assert kwargs["config"] == {"task_type": "RETRIEVAL_QUERY", "output_dimensionality": 768}


def test_ui_ai_query_embeds_the_question_with_768_dimensions(embeddings_file):
    client = _stub_client()
    with (
        patch.object(main, "_get_gemini_api_key", return_value="key"),
        patch.object(main, "_get_gemini_default_model", return_value="gemini-test"),
        patch.object(main.genai, "Client", return_value=client),
        patch.object(
            main,
            "_duckdb_connection",
            side_effect=lambda **_: _LocalFileConnection(embeddings_file),
        ),
    ):
        result = main._ai_query("What is the pool status?")
    assert result["ok"], result
    _assert_question_is_embedded_like_the_pipeline(client)
    assert result["snippets"][0]["summary"]


class _LocalFileConnection:
    """DuckDB connection that redirects the embeddings s3 glob to a local file."""

    def __init__(self, path: str):
        self._con = duckdb.connect()
        self._path = path

    def execute(self, sql, params=None):
        params = [self._path if str(p).startswith("s3://") else p for p in (params or [])]
        return self._con.execute(sql, params)

    def close(self):
        self._con.close()


def test_backend_rag_search_embeds_the_question_with_768_dimensions(embeddings_file):
    client = _stub_client()
    with (
        patch.object(ai_service, "_resolve_api_key", return_value="key"),
        patch.object(ai_service.genai, "Client", return_value=client),
        patch.object(
            ai_service,
            "create_duckdb_connection",
            side_effect=lambda **_: _LocalFileConnection(embeddings_file),
        ),
    ):
        result = ai_service.run_rag_search("What is the pool status?")
    assert result["ok"], result
    _assert_question_is_embedded_like_the_pipeline(client)
    assert result["snippets"][0]["summary"]
