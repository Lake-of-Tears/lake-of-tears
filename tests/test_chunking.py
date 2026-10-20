"""Chunking strategies for the embedding pipeline (pipeline/embed/chunking)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

root = Path(__file__).parents[1]
for sub in ("pipeline/embed", "pipeline/shared"):
    if str(root / sub) not in sys.path:
        sys.path.insert(0, str(root / sub))

from chunking import (  # noqa: E402
    STRATEGIES,
    AgenticChunker,
    DocumentChunker,
    RecursiveChunker,
    explode_chunks,
    get_chunker,
)
from chunking.agentic import parse_propositions  # noqa: E402


def numbered_text(paragraphs: int = 6, sentences: int = 5) -> str:
    """Every sentence is unique and numbered, so missing or repeated text is easy to spot."""
    return "\n\n".join(
        " ".join(f"Sentence p{p}s{s} says something specific." for s in range(sentences))
        for p in range(paragraphs)
    )


# ── registry ──────────────────────────────────────────────────────────────────


def test_the_three_strategies_are_registered_and_recursive_is_the_default():
    assert set(STRATEGIES) == {"recursive", "document", "agentic"}
    assert isinstance(get_chunker(), RecursiveChunker)
    assert isinstance(get_chunker(None), RecursiveChunker)
    assert isinstance(get_chunker("Document"), DocumentChunker)
    assert isinstance(get_chunker("agentic", generate=lambda *a, **k: "[]"), AgenticChunker)


def test_unknown_strategy_and_bad_options_are_clear_errors():
    with pytest.raises(ValueError, match="Unknown chunking strategy 'semantic'"):
        get_chunker("semantic")
    with pytest.raises(ValueError, match="Invalid options for the 'document'"):
        get_chunker("document", chunk_size=10)


# ── document ──────────────────────────────────────────────────────────────────


def test_document_chunker_keeps_the_whole_row_as_one_chunk():
    text = "Weather 2026-10-16: 50F\n\nwith a blank line and more than 512 characters " + "x" * 600
    assert DocumentChunker().chunk(text) == [text]


@pytest.mark.parametrize("blank", ["", "   ", "\n\n", None, float("nan")])
def test_blank_values_give_no_chunks_for_every_strategy(blank):
    for chunker in (
        DocumentChunker(),
        RecursiveChunker(),
        AgenticChunker(generate=lambda *a, **k: "[]"),
    ):
        assert chunker.chunk(blank) == []


# ── recursive ─────────────────────────────────────────────────────────────────


def test_short_text_is_a_single_chunk():
    assert RecursiveChunker(chunk_size=512).chunk("A short note.") == ["A short note."]


def test_text_exactly_chunk_size_is_not_split():
    text = "a" * 100
    assert RecursiveChunker(chunk_size=100, chunk_overlap=10).chunk(text) == [text]


def test_no_chunk_is_longer_than_chunk_size():
    for size, overlap in ((80, 0), (80, 20), (120, 30), (512, 64), (7, 2), (1, 0)):
        for chunk in RecursiveChunker(size, overlap).chunk(numbered_text()):
            assert 0 < len(chunk) <= size, (size, overlap, len(chunk))


def test_every_chunk_is_text_from_the_document_and_nothing_is_lost():
    text = numbered_text()
    chunks = RecursiveChunker(chunk_size=150, chunk_overlap=40).chunk(text)
    assert len(chunks) > 3
    assert all(chunk in text for chunk in chunks)  # contiguous, unaltered text
    for p in range(6):
        for s in range(5):
            assert any(f"p{p}s{s} " in chunk for chunk in chunks), f"lost sentence p{p}s{s}"


def test_without_overlap_each_sentence_appears_exactly_once():
    chunks = RecursiveChunker(chunk_size=150, chunk_overlap=0).chunk(numbered_text())
    joined = "\n".join(chunks)
    for p in range(6):
        for s in range(5):
            assert joined.count(f"p{p}s{s} ") == 1


def test_overlap_repeats_text_across_a_boundary_but_never_more_than_allowed():
    overlap = 45
    chunks = RecursiveChunker(chunk_size=150, chunk_overlap=overlap).chunk(numbered_text(1, 12))
    assert len(chunks) > 2
    for before, after in zip(chunks, chunks[1:], strict=False):
        shared = max((k for k in range(1, len(after) + 1) if before.endswith(after[:k])), default=0)
        assert 0 < shared <= overlap, (before, after, shared)


def test_cuts_prefer_paragraph_then_sentence_boundaries():
    paragraphs = ["First paragraph. " * 3, "Second paragraph. " * 3]
    chunks = RecursiveChunker(chunk_size=70, chunk_overlap=0).chunk("\n\n".join(paragraphs))
    assert not any("First" in c and "Second" in c for c in chunks)  # never crosses the paragraph
    assert all(c.endswith(".") for c in chunks)  # and never cuts a sentence in half


def test_a_word_longer_than_chunk_size_is_still_cut_down():
    chunks = RecursiveChunker(chunk_size=10, chunk_overlap=0).chunk("x" * 35)
    assert [len(c) for c in chunks] == [10, 10, 10, 5]


def test_text_with_no_separators_and_unicode_is_handled():
    text = "日本語のテキスト" * 20  # no spaces at all
    chunks = RecursiveChunker(chunk_size=30, chunk_overlap=5).chunk(text)
    assert all(0 < len(c) <= 30 for c in chunks)
    assert "".join(c for c in chunks).count("日本語") >= 20


def test_splitting_is_deterministic():
    chunker = RecursiveChunker(chunk_size=100, chunk_overlap=20)
    assert chunker.chunk(numbered_text()) == chunker.chunk(numbered_text())


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (-5, 0), (10, -1), (10, 10), (10, 25)])
def test_bad_sizes_are_rejected(size, overlap):
    with pytest.raises(ValueError):
        RecursiveChunker(chunk_size=size, chunk_overlap=overlap)


# ── agentic ───────────────────────────────────────────────────────────────────


def test_agentic_uses_the_propositions_the_model_returns():
    calls = []

    def fake(prompt, system=None, temperature=None, **kw):
        calls.append((prompt, system, temperature))
        return '```json\n["Acme was founded in 1999.", "Acme sells anvils."]\n```'

    chunker = AgenticChunker(generate=fake)
    assert chunker.chunk("Acme, founded in 1999, sells anvils.") == [
        "Acme was founded in 1999.",
        "Acme sells anvils.",
    ]
    assert "Acme, founded in 1999" in calls[0][0]
    assert "proposition" in calls[0][1] and calls[0][2] == 0.0


def test_agentic_sends_long_documents_in_windows():
    prompts = []

    def fake(prompt, **kw):
        prompts.append(prompt)
        return json.dumps([f"window {len(prompts)}"])

    chunks = AgenticChunker(window=200, generate=fake).chunk(numbered_text(6, 5))
    assert len(prompts) > 1
    assert all(len(p) < 200 + 120 for p in prompts)  # window plus the instruction line
    assert chunks == [f"window {i}" for i in range(1, len(prompts) + 1)]


@pytest.mark.parametrize(
    "bad_reply", ["I cannot do that", "[1, 2, 3]", '{"a": 1}', "[unclosed", "", "[]"]
)
def test_agentic_falls_back_to_recursive_when_the_reply_is_unusable(bad_reply):
    text = "Plain document text. " * 5
    chunker = AgenticChunker(generate=lambda *a, **k: bad_reply)
    assert chunker.chunk(text) == RecursiveChunker().chunk(text)


def test_agentic_falls_back_when_the_model_call_fails():
    def broken(*args, **kwargs):
        raise RuntimeError("AI provider down")

    text = "Some document text."
    assert AgenticChunker(generate=broken).chunk(text) == [text]


def test_parse_propositions_accepts_fences_and_chatter():
    assert parse_propositions('Sure! ["a", " b "] hope that helps') == ["a", "b"]
    assert parse_propositions('```\n["x"]\n```') == ["x"]
    assert parse_propositions('["a", ""]') == ["a"]
    assert parse_propositions("nothing") is None


# ── exploding a DataFrame ─────────────────────────────────────────────────────


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": "doc-a", "title": "Long", "body": numbered_text(3, 4), "views": 3, "note": None},
            {"id": "doc-b", "title": "Short", "body": "Tiny.", "views": 1, "note": "n"},
            {"id": "doc-c", "title": "Empty", "body": "   ", "views": 0, "note": None},
        ]
    )


def test_each_document_becomes_one_row_per_chunk_with_lineage():
    df = frame()
    out = explode_chunks(
        df,
        df["body"].tolist(),
        df["id"].tolist(),
        RecursiveChunker(150, 30),
        exclude_metadata=["body"],
    )
    long = out[out["parent_id"] == "doc-a"]
    assert len(long) > 1
    assert long["chunk_index"].tolist() == list(range(len(long)))
    assert (long["total_chunks"] == len(long)).all()
    assert long["id"].tolist() == [f"doc-a:{i}" for i in range(len(long))]
    assert out.columns.tolist() == [
        "id", "parent_id", "chunk_index", "total_chunks", "content", "metadata"
    ]  # fmt: skip


def test_a_one_chunk_document_keeps_its_own_id_and_blank_documents_are_dropped():
    df = frame()
    out = explode_chunks(df, df["body"].tolist(), df["id"].tolist(), RecursiveChunker(150, 30))
    short = out[out["parent_id"] == "doc-b"].iloc[0]
    assert (short["id"], short["chunk_index"], short["total_chunks"], short["content"]) == (
        "doc-b", 0, 1, "Tiny."
    )  # fmt: skip
    assert "doc-c" not in set(out["parent_id"])


def test_chunks_inherit_the_parents_metadata_without_text_or_missing_values():
    df = frame()
    out = explode_chunks(
        df, df["body"].tolist(), df["id"].tolist(), RecursiveChunker(150, 30),
        exclude_metadata=["id", "body"],
    )  # fmt: skip
    long_meta = {
        json.dumps(m) for m in out[out["parent_id"] == "doc-a"]["metadata"].map(json.loads)
    }
    assert len(long_meta) == 1  # identical on every chunk of the document
    assert json.loads(out[out["parent_id"] == "doc-a"].iloc[0]["metadata"]) == {
        "title": "Long",
        "views": 3,
    }  # no body, no id, and the missing note is left out
    assert json.loads(out[out["parent_id"] == "doc-b"].iloc[0]["metadata"])["note"] == "n"


def test_long_metadata_values_are_shortened():
    df = pd.DataFrame([{"id": "x", "body": "text", "blob": "z" * 1000}])
    out = explode_chunks(
        df, df["body"].tolist(), ["x"], DocumentChunker(), exclude_metadata=["body"]
    )
    assert len(json.loads(out.iloc[0]["metadata"])["blob"]) <= 201


def test_an_empty_frame_gives_an_empty_result_with_the_right_columns():
    out = explode_chunks(pd.DataFrame({"body": []}), [], [], RecursiveChunker())
    assert out.empty and "parent_id" in out.columns


def test_the_text_and_id_lists_must_line_up_with_the_rows():
    df = frame()
    with pytest.raises(ValueError):
        explode_chunks(df, df["body"].tolist()[:2], df["id"].tolist(), RecursiveChunker())
