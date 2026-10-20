"""Runs the real ``google-genai`` SDK against a local stub server.

Dependency bumps of the SDK are only safe if our calls still build valid requests and the replies
still parse, which mocks cannot show. No network or API key is used.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("google.genai")

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from ai_providers.gemini import GeminiProvider  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

DIMENSION = 8


class _Stub(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict, str | None]] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        self.seen.append((self.path, body, self.headers.get("x-goog-api-key")))
        if "embed" in self.path.lower():
            count = len(body.get("requests", [])) or 1
            out = {"embeddings": [{"values": [0.1] * DIMENSION} for _ in range(count)]}
        else:
            parts = [{"text": " pong "}]
            out = {
                "candidates": [
                    {"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}
                ]
            }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def provider():
    _Stub.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"

    def factory(key: str):
        return genai.Client(api_key=key, http_options=types.HttpOptions(base_url=url))

    yield GeminiProvider(api_key="test-key-123", dimension=DIMENSION, client_factory=factory)
    server.shutdown()


def test_generate_sends_system_prompt_and_temperature_and_parses_the_reply(provider):
    assert provider.generate("ping", system="be brief", temperature=0.2) == "pong"
    path, body, key = _Stub.seen[-1]
    assert path.endswith(":generateContent")
    assert key == "test-key-123"
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert body["generationConfig"]["temperature"] == 0.2


def test_embeddings_return_one_vector_per_text_of_the_configured_size(provider):
    vectors = provider.embed_texts(["a", "b"])
    assert [len(v) for v in vectors] == [DIMENSION, DIMENSION]
    path, body, _ = _Stub.seen[-1]
    assert "embed" in path.lower()
    assert len(body["requests"]) == 2


def test_query_embeddings_use_the_query_task_type(provider):
    provider.embed_texts(["question"], query=True)
    _, body, _ = _Stub.seen[-1]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert body["requests"][0]["outputDimensionality"] == DIMENSION
