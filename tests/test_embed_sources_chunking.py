"""embed_sources with chunking: per-source defaults, text detection, and what gets written."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

pd = pytest.importorskip("pandas")

root = Path(__file__).parents[1]
for sub in ("pipeline/shared", "pipeline/embed"):
    if str(root / sub) not in sys.path:
        sys.path.insert(0, str(root / sub))

import ai_gateway  # noqa: E402
import embed_sources  # noqa: E402
from chunking import DocumentChunker, RecursiveChunker  # noqa: E402

DIM = 4


def sentences(n: int) -> str:
    return " ".join(f"Sentence number {i} explains one thing clearly." for i in range(n))


# ── configuration and defaults ────────────────────────────────────────────────


@pytest.mark.parametrize("source", ["events", "audit_logs", "webhook_payloads"])
def test_shipped_structured_sources_stay_record_level(source):
    config = embed_sources.source_config(source)
    assert config["chunking"] == "document"
    assert isinstance(embed_sources.build_chunker(config), DocumentChunker)


def test_structured_sources_with_a_text_function_default_to_document():
    config = embed_sources.source_config("events", "raw/events/**/*.parquet")
    assert config["chunking"] == "document"
    assert config["glob"] == "raw/events/**/*.parquet"


def test_an_unregistered_text_source_defaults_to_recursive_512_64():
    config = embed_sources.source_config("company_wiki", "raw/company_wiki/**/*.parquet")
    chunker = embed_sources.build_chunker(config)
    assert isinstance(chunker, RecursiveChunker)
    assert (chunker.chunk_size, chunker.chunk_overlap) == (512, 64)


def test_a_source_can_set_its_own_strategy_and_sizes(monkeypatch):
    monkeypatch.setitem(
        embed_sources.SOURCES,
        "wiki",
        {
            "glob": "raw/wiki/**/*.parquet",
            "chunking": "recursive",
            "chunk_size": 200,
            "chunk_overlap": 20,
        },
    )
    chunker = embed_sources.build_chunker(embed_sources.source_config("wiki"))
    assert (chunker.chunk_size, chunker.chunk_overlap) == (200, 20)


def test_a_plain_glob_string_in_sources_is_still_accepted(monkeypatch):
    monkeypatch.setitem(embed_sources.SOURCES, "legacy", "raw/legacy/**/*.parquet")
    config = embed_sources.source_config("legacy")
    assert config["glob"] == "raw/legacy/**/*.parquet"
    assert config["chunking"] == "recursive"


def test_an_unknown_strategy_name_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(embed_sources.SOURCES, "bad", {"glob": "g", "chunking": "semantic"})
    with pytest.raises(ValueError, match="Unknown chunking strategy"):
        embed_sources.build_chunker(embed_sources.source_config("bad"))


# ── where the text comes from ─────────────────────────────────────────────────


def test_text_column_is_detected_then_overridable_then_falls_back_to_all_text_columns():
    df = pd.DataFrame({"id": ["1"], "title": ["T"], "body": ["The body."], "n": [5]})
    assert embed_sources.source_text(df, {})[0].tolist() == ["The body."]  # body beats title
    assert embed_sources.source_text(df, {"text_column": "title"})[0].tolist() == ["T"]
    only_other = pd.DataFrame({"id": ["1"], "a": ["x"], "b": ["y"], "n": [5]})
    text, column = embed_sources.source_text(only_other, {})
    assert column is None and text.tolist() == ["1 | x | y"]


# ── what gets written ─────────────────────────────────────────────────────────


@pytest.fixture
def run_embed(monkeypatch):
    """Run embed_source on a given DataFrame with a stub embedder and a fake DuckDB connection."""

    def run(source: str, raw: pd.DataFrame, glob: str | None = None) -> pd.DataFrame:
        con = MagicMock()
        con.execute.return_value.df.return_value = raw
        registered: dict[str, pd.DataFrame] = {}
        con.register.side_effect = lambda name, df: registered.__setitem__(name, df)
        monkeypatch.setattr(embed_sources, "get_duckdb_con", lambda: con)
        monkeypatch.setattr(
            embed_sources,
            "date",
            type("D", (), {"today": staticmethod(lambda: datetime.date(2026, 10, 17))}),
        )

        def fake_embed(texts, query=False):
            return {
                "embeddings": [[float(len(t) % 7), 1.0, 0.0, 0.5] for t in texts],
                "provider": "stub",
                "embedding_model": "stub-embed",
                "dimension": DIM,
            }

        monkeypatch.setattr(embed_sources, "embed_texts_with_meta", fake_embed)
        embed_sources.embed_source(source, glob)
        return registered["out_df"]

    return run


def test_a_long_document_is_split_and_every_chunk_is_embedded_with_lineage(run_embed):
    raw = pd.DataFrame(
        [
            {
                "id": "wiki-1",
                "title": "Deploy guide",
                "body": sentences(40),
                "source_date": "2026-10-16",
            },
            {
                "id": "wiki-2",
                "title": "Note",
                "body": "A very short page.",
                "source_date": "2026-10-16",
            },
        ]
    )
    out = run_embed("company_wiki", raw, "raw/company_wiki/**/*.parquet")
    long, short = out[out["parent_id"] == "wiki-1"], out[out["parent_id"] == "wiki-2"]
    assert len(long) > 3 and len(short) == 1
    assert long["chunk_index"].tolist() == list(range(len(long)))
    assert (long["total_chunks"] == len(long)).all()
    assert long["id"].is_unique and long["content"].str.len().max() <= 512
    assert short.iloc[0]["id"] == "wiki-2" and short.iloc[0]["total_chunks"] == 1
    assert all(len(v) == DIM for v in out["embedding"])
    assert set(out["source"]) == {"company_wiki"} and set(out["source_date"]) == {"2026-10-16"}
    assert (out["provider"] == "stub").all() and (out["dimension"] == DIM).all()
    assert json.loads(long.iloc[0]["metadata"]) == {"title": "Deploy guide"}


def test_folder_partition_columns_are_not_copied_into_metadata(run_embed):
    raw = pd.DataFrame(
        [
            {"id": "w1", "title": "T", "body": "Some text.", "year": 2026, "month": 10, "day": 16,
             "source_date": "2026-10-16"}
        ]
    )  # fmt: skip
    out = run_embed("company_wiki", raw, "raw/company_wiki/**/*.parquet")
    assert json.loads(out.iloc[0]["metadata"]) == {"title": "T"}


def test_structured_rows_keep_one_chunk_and_their_old_ids(run_embed):
    raw = pd.DataFrame(
        [{"event_type": "user_login", "action": "login_success", "source_date": "2026-10-16"}]
    )
    out = run_embed("events", raw, "raw/events/**/*.parquet")
    assert len(out) == 1
    row = out.iloc[0]
    assert row["id"] == row["parent_id"] and (row["chunk_index"], row["total_chunks"]) == (0, 1)
    assert row["content"].startswith("user_login")


def test_a_run_with_no_text_writes_nothing(run_embed):
    raw = pd.DataFrame([{"id": "a", "body": "   ", "source_date": "2026-10-16"}])
    with pytest.raises(KeyError):  # nothing was registered, so there is no output frame
        run_embed("company_wiki", raw, "raw/company_wiki/**/*.parquet")


# ── search still works across old and new files ───────────────────────────────


def test_vector_search_reads_chunked_files_next_to_files_written_before_chunking(
    run_embed, tmp_path
):
    raw = pd.DataFrame([{"id": "wiki-1", "body": sentences(30), "source_date": "2026-10-16"}])
    new = run_embed("company_wiki", raw, "raw/company_wiki/**/*.parquet")
    con = duckdb.connect()
    con.register("new_df", new)
    con.execute(f"COPY new_df TO '{tmp_path / 'chunked.parquet'}' (FORMAT PARQUET)")
    # a file from before this change: no provider/model, no lineage columns
    con.execute(
        f"""COPY (SELECT 'old-1' AS id, 'weather' AS source, 'Weather yesterday' AS content,
                         [0.0, 1.0, 0.0, 0.5]::DOUBLE[] AS embedding, now()::TIMESTAMP AS ingested_at)
            TO '{tmp_path / 'legacy.parquet'}' (FORMAT PARQUET)"""
    )
    sql = ai_gateway.vector_search_sql(DIM, has_model_column=False)
    rows = con.execute(sql, [[0.0, 1.0, 0.0, 0.5], str(tmp_path / "*.parquet"), 100]).fetchall()
    contents = [r[2] for r in rows]
    assert "Weather yesterday" in contents
    assert sum(1 for c in contents if c.startswith("Sentence number")) == len(new)
    lineage = con.execute(
        f"SELECT count(*), count(parent_id) FROM read_parquet('{tmp_path}/*.parquet', union_by_name=true)"
    ).fetchone()
    assert lineage == (len(new) + 1, len(new))  # the old row simply has no parent_id
