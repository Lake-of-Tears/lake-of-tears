from __future__ import annotations

import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pytest

root = Path(__file__).parents[1]
for sub in ("pipeline/shared", "pipeline/embed"):
    if str(root / sub) not in sys.path:
        sys.path.insert(0, str(root / sub))

import ai_gateway  # noqa: E402
import embed_sources  # noqa: E402


class _Gateway:
    """A tiny HTTP server standing in for the backend AI gateway."""

    def __init__(self, dimension: int = 4):
        self.requests: list[dict] = []
        self.dimension = dimension
        self.fail_with: tuple[int, str] | None = None
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, body: dict):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                gateway.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "secret": self.headers.get("X-Internal-Secret"),
                        "body": body,
                    }
                )
                if gateway.fail_with:
                    status, detail = gateway.fail_with
                    return self._reply(status, {"detail": detail})
                meta = {
                    "provider": "stub",
                    "embedding_model": "stub-embed",
                    "dimension": gateway.dimension,
                }
                if self.path.endswith("/info"):
                    return self._reply(200, {**meta, "chat_model": "stub-chat"})
                if self.path.endswith("/embed"):
                    vecs = [[1.0] * gateway.dimension for _ in body["texts"]]
                    return self._reply(200, {"embeddings": vecs, **meta})
                return self._reply(200, {"text": f"echo:{body['prompt']}", **meta})

            do_GET = do_POST = _handle

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway(monkeypatch):
    gw = _Gateway()
    monkeypatch.setenv("BACKEND_URL", gw.url)
    monkeypatch.setenv("AUTH_SECRET_KEY", "internal-secret")
    yield gw
    gw.close()


# ── client ────────────────────────────────────────────────────────────────────


def test_client_sends_the_internal_secret_and_returns_embeddings_with_metadata(gateway):
    result = ai_gateway.embed_texts_with_meta(["a", "b"])
    assert result["embeddings"] == [[1.0] * 4, [1.0] * 4]
    assert (result["provider"], result["embedding_model"], result["dimension"]) == (
        "stub",
        "stub-embed",
        4,
    )
    req = gateway.requests[0]
    assert req["path"] == "/api/internal/ai/embed"
    assert req["secret"] == "internal-secret"
    assert req["body"] == {"texts": ["a", "b"], "query": False}


def test_client_splits_large_requests_into_batches(gateway):
    texts = [f"t{i}" for i in range(250)]
    result = ai_gateway.embed_texts_with_meta(texts)
    assert len(result["embeddings"]) == 250
    assert [len(r["body"]["texts"]) for r in gateway.requests] == [100, 100, 50]


def test_query_embedding_flag_and_generate_and_info(gateway):
    ai_gateway.embed_query("question")
    assert gateway.requests[-1]["body"]["query"] is True
    assert ai_gateway.embed_texts(["x"], task_type="RETRIEVAL_QUERY") == [[1.0] * 4]
    assert ai_gateway.generate("hello", system="s", model="m", temperature=0.1) == "echo:hello"
    assert gateway.requests[-1]["body"] == {
        "prompt": "hello",
        "system": "s",
        "model": "m",
        "temperature": 0.1,
    }
    assert ai_gateway.provider_info()["chat_model"] == "stub-chat"


def test_client_reports_gateway_errors_with_the_detail(gateway):
    gateway.fail_with = (503, "GEMINI_API_KEY is not set.")
    with pytest.raises(ai_gateway.AIGatewayError, match="503: GEMINI_API_KEY is not set"):
        ai_gateway.generate("hi")


def test_client_reports_an_unreachable_gateway(monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9")
    with pytest.raises(ai_gateway.AIGatewayError, match="unreachable"):
        ai_gateway.provider_info()


# ── embed_sources records which model made the vectors ───────────────────────


def test_embed_source_writes_provider_model_and_dimension(gateway, monkeypatch):
    raw = pd.DataFrame(
        [{"timestamp": "2026-10-16T01:00", "temperature_2m": 50, "source_date": "2026-10-16"}]
    )
    con = MagicMock()
    con.execute.return_value.df.return_value = raw
    registered: dict[str, pd.DataFrame] = {}
    con.register.side_effect = lambda name, df: registered.__setitem__(name, df)
    monkeypatch.setattr(embed_sources, "get_duckdb_con", lambda: con)
    monkeypatch.setattr(
        embed_sources,
        "date",
        type("D", (), {"today": staticmethod(lambda: __import__("datetime").date(2026, 10, 17))}),
    )
    embed_sources.embed_source("weather", "raw/weather/**/*.parquet")
    out = registered["out_df"]
    assert list(out.columns) == [
        "id",
        "source",
        "content",
        "embedding",
        "source_date",
        "provider",
        "model",
        "dimension",
        "ingested_at",
    ]
    row = out.iloc[0]
    assert (row["provider"], row["model"], row["dimension"]) == ("stub", "stub-embed", 4)
    assert len(row["embedding"]) == 4


# ── vector search over mixed models and dimensions ───────────────────────────


def _unit(seed: int, dim: int) -> list[float]:
    raw = [math.sin(seed * 12.9898 + i * 0.7) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


@pytest.fixture
def mixed_lake(tmp_path: Path) -> str:
    """Legacy 768-d files (no model column), a different 768-d model, and a 1536-d model."""
    con = duckdb.connect()

    def write(name: str, rows: list[tuple], with_meta: bool):
        cols = (
            "id VARCHAR, source VARCHAR, content VARCHAR, embedding DOUBLE[], ingested_at TIMESTAMP"
        )
        if with_meta:
            cols += ", model VARCHAR"
        con.execute(f"CREATE OR REPLACE TABLE t ({cols})")
        for r in rows:
            con.execute(f"INSERT INTO t VALUES ({', '.join('?' for _ in r)})", list(r))
        con.execute(f"COPY t TO '{tmp_path / name}' (FORMAT PARQUET)")

    write(
        "legacy.parquet",
        [("1", "w", "legacy row", _unit(1, 768), "2026-10-16 00:00:00")],
        with_meta=False,
    )
    write(
        "other768.parquet",
        [("2", "w", "other 768 row", _unit(1, 768), "2026-10-16 00:00:00", "other-768")],
        with_meta=True,
    )
    write(
        "big.parquet",
        [("3", "w", "big row", _unit(1, 1536), "2026-10-16 00:00:00", "big-1536")],
        with_meta=True,
    )
    return str(tmp_path / "*.parquet")


def _search(glob: str, dimension: int, model: str, has_model: bool = True) -> list[str]:
    params = [_unit(1, dimension), glob] + ([model] if has_model else []) + [10]
    rows = (
        duckdb.connect()
        .execute(ai_gateway.vector_search_sql(dimension, has_model), params)
        .fetchall()
    )
    return [r[2] for r in rows]


def test_search_only_compares_vectors_of_the_active_model_and_dimension(mixed_lake):
    assert _search(mixed_lake, 768, "gemini-embedding-001") == ["legacy row"]
    assert _search(mixed_lake, 768, "other-768") == ["other 768 row"]
    assert _search(mixed_lake, 1536, "big-1536") == ["big row"]
    assert _search(mixed_lake, 3072, "none") == []


def test_search_works_when_no_file_has_a_model_column(tmp_path: Path):
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (id VARCHAR, source VARCHAR, content VARCHAR, embedding DOUBLE[], "
        "ingested_at TIMESTAMP)"
    )
    con.execute("INSERT INTO t VALUES ('1', 'w', 'only legacy', ?, now())", [_unit(1, 768)])
    con.execute(f"COPY t TO '{tmp_path / 'a.parquet'}' (FORMAT PARQUET)")
    assert _search(str(tmp_path / "*.parquet"), 768, "ignored", has_model=False) == ["only legacy"]
