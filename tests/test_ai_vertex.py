from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ai_providers.base import ProviderError, ProviderNotConfigured
from ai_providers.registry import PROVIDERS, build_provider, public_provider_config
from ai_providers.vertex import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    VertexAIProvider,
    normalize_vertex_model_name,
)

SAMPLE_SA_KEY = {
    "type": "service_account",
    "project_id": "test-vertex-project-123",
    "private_key_id": "key123456",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC3\n-----END PRIVATE KEY-----\n",
    "client_email": "vertex-sa@test-vertex-project-123.iam.gserviceaccount.com",
    "client_id": "1234567890",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def test_normalize_vertex_model_name():
    assert (
        normalize_vertex_model_name("publishers/google/models/gemini-2.5-flash")
        == "gemini-2.5-flash"
    )
    assert normalize_vertex_model_name("models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert normalize_vertex_model_name("gemini-2.5-flash") == "gemini-2.5-flash"
    assert normalize_vertex_model_name("") == ""


def test_vertex_provider_init_with_service_account():
    sa_json = json.dumps(SAMPLE_SA_KEY)
    with patch("google.oauth2.service_account.Credentials.from_service_account_info") as mock_sa:
        mock_creds = MagicMock()
        mock_sa.return_value = mock_creds

        provider = VertexAIProvider(
            service_account_json=sa_json,
            location="us-east4",
            chat_model="gemini-2.5-pro",
            embedding_model="text-embedding-005",
            dimension=768,
        )

        assert provider.project_id == "test-vertex-project-123"
        assert provider.location == "us-east4"
        assert provider.chat_model == "gemini-2.5-pro"
        assert provider.embedding_model == "text-embedding-005"
        assert provider.dimension == 768
        mock_sa.assert_called_once()


def test_vertex_provider_init_invalid_json():
    with pytest.raises(ProviderNotConfigured, match="Invalid Google Cloud Service Account JSON"):
        VertexAIProvider(
            project_id="test-proj",
            service_account_json="not-a-valid-json",
        )


def test_vertex_provider_init_missing_project_id():
    with patch("google.auth.default", side_effect=Exception("No ADC")):
        with pytest.raises(ProviderNotConfigured, match="project_id is required"):
            VertexAIProvider(project_id="", service_account_json="")


def test_vertex_provider_ambient_adc_resolution():
    with patch("google.auth.default", return_value=(MagicMock(), "adc-detected-project")):
        provider = VertexAIProvider(project_id="", location="us-central1")
        assert provider.project_id == "adc-detected-project"
        assert provider.location == "us-central1"


def test_vertex_provider_generate_basic():
    mock_client = MagicMock()
    mock_resp = SimpleNamespace(text="Hello from Vertex Gemini!")
    mock_client.models.generate_content.return_value = mock_resp

    provider = VertexAIProvider(
        project_id="test-project",
        client=mock_client,
    )

    ans = provider.generate("Say hello", system="You are helpful.", temperature=0.2)
    assert ans == "Hello from Vertex Gemini!"

    mock_client.models.generate_content.assert_called_once()
    kwargs = mock_client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == DEFAULT_CHAT_MODEL
    assert kwargs["contents"] == "Say hello"
    assert kwargs["config"]["system_instruction"] == "You are helpful."
    assert kwargs["config"]["temperature"] == 0.2


def test_vertex_provider_generate_fallback_chain():
    mock_client = MagicMock()
    # First model (gemini-2.5-flash) returns 404 model not found
    # Second model (gemini-2.0-flash) succeeds
    err_404 = Exception("404 Model gemini-2.5-flash not found or deprecated")
    success_resp = SimpleNamespace(text="Fell back to gemini-2.0-flash successfully")
    mock_client.models.generate_content.side_effect = [err_404, success_resp]

    fallback_hook = MagicMock()

    provider = VertexAIProvider(
        project_id="test-project",
        chat_model="gemini-2.5-flash",
        client=mock_client,
        on_fallback=fallback_hook,
    )

    ans = provider.generate("Summarize this")
    assert ans == "Fell back to gemini-2.0-flash successfully"
    assert mock_client.models.generate_content.call_count == 2

    # Verify fallback hook notified
    fallback_hook.assert_called_once()
    failed_m, fb_m, reason = fallback_hook.call_args[0]
    assert failed_m == "gemini-2.5-flash"
    assert fb_m == "gemini-2.0-flash"
    assert "404" in reason


def test_vertex_provider_generate_non_404_error_fails_immediately():
    mock_client = MagicMock()
    auth_err = Exception("403 PermissionDenied: The caller does not have permission")
    mock_client.models.generate_content.side_effect = auth_err

    fallback_hook = MagicMock()

    provider = VertexAIProvider(
        project_id="test-project",
        client=mock_client,
        on_fallback=fallback_hook,
    )

    with pytest.raises(ProviderError, match="403 PermissionDenied"):
        provider.generate("Test prompt")

    # Call count should be exactly 1: did not attempt fallback on auth failure
    assert mock_client.models.generate_content.call_count == 1
    fallback_hook.assert_not_called()


def test_vertex_provider_embed_texts():
    mock_client = MagicMock()
    mock_client.models.embed_content.return_value = SimpleNamespace(
        embeddings=[
            SimpleNamespace(values=[0.1] * 768),
            SimpleNamespace(values=[0.2] * 768),
        ]
    )

    provider = VertexAIProvider(
        project_id="test-project",
        dimension=768,
        client=mock_client,
    )

    vectors = provider.embed_texts(["doc1", "doc2"], query=False)
    assert len(vectors) == 2
    assert len(vectors[0]) == 768
    assert vectors[0][0] == 0.1

    kwargs = mock_client.models.embed_content.call_args.kwargs
    assert kwargs["model"] == DEFAULT_EMBEDDING_MODEL
    assert kwargs["contents"] == ["doc1", "doc2"]
    assert kwargs["config"]["task_type"] == "RETRIEVAL_DOCUMENT"
    assert kwargs["config"]["output_dimensionality"] == 768


def test_vertex_provider_embed_empty():
    mock_client = MagicMock()
    provider = VertexAIProvider(project_id="test-project", client=mock_client)
    res = provider.embed_texts([])
    assert res == []
    mock_client.models.embed_content.assert_not_called()


def test_vertex_provider_embed_dimension_mismatch():
    mock_client = MagicMock()
    mock_client.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.1] * 512)]
    )

    provider = VertexAIProvider(
        project_id="test-project",
        dimension=768,
        client=mock_client,
    )

    with pytest.raises(ProviderError, match="expected 768"):
        provider.embed_texts(["doc1"])


def test_vertex_provider_discover_models():
    mock_client = MagicMock()
    m1 = SimpleNamespace(
        name="publishers/google/models/gemini-2.5-flash",
        supported_actions=["generateContent"],
    )
    m2 = SimpleNamespace(
        name="publishers/google/models/gemini-2.5-pro",
        supported_actions=["generateContent"],
    )
    m3 = SimpleNamespace(
        name="publishers/google/models/text-embedding-005",
        supported_actions=["embedContent"],
    )
    m4 = SimpleNamespace(
        name="publishers/google/models/text-multilingual-embedding-002",
        supported_actions=["embedContent"],
    )
    mock_client.models.list.return_value = [m1, m2, m3, m4]

    provider = VertexAIProvider(project_id="test-project", client=mock_client)
    discovered = provider.discover_models()

    mock_client.models.list.assert_called_once_with(config={"query_base": True})
    assert "gemini-2.5-flash" in discovered["chat"]
    assert "gemini-2.5-pro" in discovered["chat"]
    assert "text-embedding-005" in discovered["embedding"]
    assert "text-multilingual-embedding-002" in discovered["embedding"]


def test_vertex_provider_test_connection_success():
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = SimpleNamespace(text="OK")
    mock_client.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.05] * 768)]
    )

    provider = VertexAIProvider(
        project_id="test-project",
        dimension=768,
        client=mock_client,
    )

    report = provider.test_connection()
    assert report["ok"] is True
    assert report["provider"] == "vertex"
    assert report["chat"]["ok"] is True
    assert report["embedding"]["ok"] is True
    assert report["embedding"]["dimension"] == 768
    assert "Connected to vertex" in report["message"]


def test_vertex_provider_test_connection_failure():
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("Vertex quota exceeded")
    mock_client.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.05] * 768)]
    )

    provider = VertexAIProvider(
        project_id="test-project",
        dimension=768,
        client=mock_client,
    )

    report = provider.test_connection()
    assert report["ok"] is False
    assert report["chat"]["ok"] is False
    assert "quota exceeded" in report["chat"]["message"]


def test_vertex_provider_error_redaction():
    sa_json = json.dumps(SAMPLE_SA_KEY)
    with patch("google.oauth2.service_account.Credentials.from_service_account_info"):
        provider = VertexAIProvider(
            project_id="test-project",
            service_account_json=sa_json,
            client=MagicMock(),
        )
        exc_with_key = Exception(f"Failed with key: {SAMPLE_SA_KEY['private_key']}")
        redacted = provider._redact(exc_with_key)
        assert "-----BEGIN PRIVATE KEY-----" not in redacted
        assert "***" in redacted


def test_vertex_registry_spec_and_build():
    assert "vertex" in PROVIDERS
    spec = PROVIDERS["vertex"]
    assert spec.name == "vertex"
    assert spec.label == "Google Cloud Vertex AI"

    field_names = [f.name for f in spec.fields]
    assert "project_id" in field_names
    assert "location" in field_names
    assert "service_account_json" in field_names
    assert "chat_model" in field_names
    assert "embedding_model" in field_names
    assert "dimension" in field_names

    # Check secret masking
    public_cfg = public_provider_config(
        "vertex",
        {
            "project_id": "my-gcp-proj",
            "service_account_json": json.dumps(SAMPLE_SA_KEY),
            "chat_model": "gemini-2.5-flash",
        },
    )
    assert public_cfg["service_account_json_configured"] is True
    assert public_cfg["service_account_json_masked"] is not None
    assert "my-gcp-proj" in public_cfg["project_id"]

    # Build provider with mock client factory
    mock_factory = MagicMock()
    prov = build_provider(
        "vertex",
        {
            "project_id": "my-gcp-proj",
            "location": "us-central1",
            "client_factory": mock_factory,
        },
    )
    assert isinstance(prov, VertexAIProvider)
    assert prov.project_id == "my-gcp-proj"
