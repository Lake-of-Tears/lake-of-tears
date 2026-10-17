from __future__ import annotations

import io
import json

import pytest
from ai_providers import PROVIDERS, ProviderError, ProviderNotConfigured, build_provider
from ai_providers.bedrock import BedrockProvider, clear_dimension_cache
from ai_providers.registry import describe_providers
from botocore.exceptions import ClientError, NoCredentialsError
from test_ai_providers import FakeDB

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class FakeRuntime:
    def __init__(self, dimension: int = 4, fail: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.dimension = dimension
        self.fail = fail

    def converse(self, **kw):
        self.calls.append(("converse", kw))
        if self.fail:
            raise self.fail
        return {"output": {"message": {"content": [{"text": " hi "}, {"text": "there "}]}}}

    def invoke_model(self, **kw):
        self.calls.append(("invoke_model", kw))
        if self.fail:
            raise self.fail
        body = json.loads(kw["body"])
        if "texts" in body:
            out = {"embeddings": {"float": [[1.0] * self.dimension for _ in body["texts"]]}}
        else:
            out = {"embedding": [0.5] * self.dimension}
        return {"body": io.BytesIO(json.dumps(out).encode())}


@pytest.fixture(autouse=True)
def _clean():
    clear_dimension_cache()
    yield
    clear_dimension_cache()


def provider(rt, **kw):
    return BedrockProvider(region="us-east-1", client=rt, **kw)


def test_chat_uses_converse_with_system_and_temperature():
    rt = FakeRuntime()
    assert provider(rt).generate("q", system="s", temperature=0.1) == "hi there"
    kw = rt.calls[0][1]
    assert kw["modelId"].startswith("anthropic.")
    assert kw["system"] == [{"text": "s"}]
    assert kw["inferenceConfig"] == {"temperature": 0.1}


def test_titan_embeds_one_text_per_call_and_passes_dimensions():
    rt = FakeRuntime(dimension=256)
    p = provider(rt, dimension=256)
    assert len(p.embed_texts(["a", "b"])) == 2
    assert [c[0] for c in rt.calls] == ["invoke_model"] * 2
    body = json.loads(rt.calls[0][1]["body"])
    assert body == {"inputText": "a", "normalize": True, "dimensions": 256}


def test_cohere_batches_and_marks_queries():
    rt = FakeRuntime(dimension=3)
    p = provider(rt, embedding_model="cohere.embed-english-v3")
    p.embed_texts(["a", "b"], query=True)
    assert len(rt.calls) == 1
    body = json.loads(rt.calls[0][1]["body"])
    assert body["input_type"] == "search_query" and body["texts"] == ["a", "b"]


def test_dimension_is_detected_once_and_checked_when_configured():
    rt = FakeRuntime(dimension=1024)
    p = provider(rt)
    assert p.dimension == 1024
    assert p.dimension == 1024
    assert len(rt.calls) == 1
    with pytest.raises(ProviderError, match="expected 512"):
        provider(FakeRuntime(dimension=1024), dimension=512).embed_texts(["x"])


def test_errors_are_friendly_and_hide_credentials():
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"no access {SECRET}"}}, "Converse"
    )
    p = provider(FakeRuntime(fail=denied), access_key_id="AKIAEXAMPLE", secret_access_key=SECRET)
    with pytest.raises(ProviderError) as exc:
        p.generate("hi")
    assert SECRET not in str(exc.value) and "AccessDenied" in str(exc.value)
    with pytest.raises(ProviderError, match="no AWS credentials"):
        provider(FakeRuntime(fail=NoCredentialsError())).generate("hi")


def test_test_connection_reports_both_parts():
    result = provider(FakeRuntime(dimension=1024), dimension=1024).test_connection()
    assert result["ok"] and result["embedding"]["dimension"] == 1024


def test_credentials_must_be_both_or_neither():
    with pytest.raises(ProviderNotConfigured):
        BedrockProvider(region="us-east-1", access_key_id="AKIA", client=FakeRuntime())
    with pytest.raises(ProviderNotConfigured, match="region"):
        BedrockProvider(region="")


def test_registry_lists_bedrock_and_masks_both_secrets():
    assert "bedrock" in PROVIDERS
    assert PROVIDERS["bedrock"].secret_fields == ("secret_access_key", "session_token")
    assert build_provider("bedrock", {"region": "eu-west-1"}).chat_model.startswith("anthropic.")
    assert SECRET not in json.dumps(describe_providers(FakeDB()))
