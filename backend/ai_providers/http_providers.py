"""Providers that talk to a model server over HTTP: Ollama's native API and OpenAI-compatible APIs.

Both work with local, self-hosted models (Ollama, vLLM, LocalAI, LM Studio, llama.cpp) and with any
hosted service that speaks the OpenAI API (Groq, DeepSeek, ...), so no data has to leave your network.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx

from ai_providers.base import AIProvider, ProviderError, ProviderNotConfigured

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 180.0  # local models can be slow, especially on the first call (model load)

# Embedding dimension discovered by probing a model, so it is only measured once per model.
_PROBED_DIMENSIONS: dict[tuple[str, str, str], int] = {}
_PROBE_LOCK = threading.Lock()


def clear_dimension_cache() -> None:
    with _PROBE_LOCK:
        _PROBED_DIMENSIONS.clear()


class _HttpProvider(AIProvider):
    """Shared plumbing: base URL, optional bearer token, error messages, dimension handling."""

    name = "http"
    label = "HTTP model server"

    def __init__(
        self,
        base_url: str,
        chat_model: str,
        embedding_model: str,
        api_key: str = "",
        dimension: int | None = None,
        query_prefix: str = "",
        document_prefix: str = "",
        transport: httpx.BaseTransport | None = None,
    ):
        if not (base_url or "").strip():
            raise ProviderNotConfigured(f"{self.label}: the base URL is not set.")
        if not (chat_model or "").strip():
            raise ProviderNotConfigured(f"{self.label}: the chat model is not set.")
        if not (embedding_model or "").strip():
            raise ProviderNotConfigured(f"{self.label}: the embedding model is not set.")
        self._base_url = self._normalise_base(base_url.strip())
        self._chat_model = chat_model.strip()
        self._embedding_model = embedding_model.strip()
        self._api_key = (api_key or "").strip()
        self._configured_dimension = int(dimension) if dimension else None
        self._query_prefix = query_prefix or ""
        self._document_prefix = document_prefix or ""
        self._transport = transport

    # -- subclass hooks ----------------------------------------------------------------
    @staticmethod
    def _normalise_base(url: str) -> str:
        return url.rstrip("/")

    # -- AIProvider ---------------------------------------------------------------------
    @property
    def chat_model(self) -> str:
        return self._chat_model

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    @property
    def dimension(self) -> int:
        if self._configured_dimension:
            return self._configured_dimension
        key = (self.name, self._base_url, self._embedding_model)
        with _PROBE_LOCK:
            known = _PROBED_DIMENSIONS.get(key)
        if known:
            return known
        # Measure it once by embedding a short text with the configured model.
        found = len(self._embed_raw([self._document_prefix + "dimension probe"])[0])
        with _PROBE_LOCK:
            _PROBED_DIMENSIONS[key] = found
        return found

    def embed_texts(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        prefix = self._query_prefix if query else self._document_prefix
        vectors = self._embed_raw([prefix + t for t in texts])
        lengths = {len(v) for v in vectors}
        if len(lengths) != 1:
            raise ProviderError(f"{self._embedding_model} returned vectors of different lengths")
        found = lengths.pop()
        if self._configured_dimension and found != self._configured_dimension:
            raise ProviderError(
                f"{self._embedding_model} returned {found} dimensions, "
                f"expected {self._configured_dimension}"
            )
        if not self._configured_dimension:
            with _PROBE_LOCK:  # learned for free from a real call: no separate probe needed later
                _PROBED_DIMENSIONS[(self.name, self._base_url, self._embedding_model)] = found
        return vectors

    # -- HTTP ---------------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                transport=self._transport,
            ) as client:
                resp = client.post(url, json=body, headers=self._headers())
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"Could not connect to {self._base_url}. Is the server running and reachable "
                f"from the backend? ({self._redact(str(exc))})"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Timed out waiting for {self._base_url}{path}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self._redact(f"Request to {url} failed: {exc}")) from exc
        if resp.status_code >= 400:
            raise ProviderError(self._http_error(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{url} did not return JSON") from exc

    def _http_error(self, resp: httpx.Response) -> str:
        detail = ""
        try:
            data = resp.json()
            err = data.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except Exception:
            detail = resp.text
        detail = self._redact((detail or resp.reason_phrase or "").strip())[:300]
        hint = ""
        if resp.status_code == 404:
            hint = " (check the model name, and that the model has been pulled/loaded)"
        elif resp.status_code in (401, 403):
            hint = " (check the API key / token)"
        return f"{self.label} returned HTTP {resp.status_code}: {detail}{hint}"

    def _embed_raw(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OpenAICompatibleProvider(_HttpProvider):
    """Any server that implements the OpenAI API: vLLM, LocalAI, LM Studio, Groq, DeepSeek, ..."""

    name = "openai_compatible"
    label = "OpenAI-compatible server"

    @staticmethod
    def _normalise_base(url: str) -> str:
        url = url.rstrip("/")
        return url if url.endswith("/v1") else f"{url}/v1"

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body: dict[str, Any] = {"model": model or self._chat_model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        data = self._post("/chat/completions", body)
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Unexpected response from /chat/completions") from exc

    def _embed_raw(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/embeddings", {"model": self._embedding_model, "input": texts})
        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [list(map(float, d["embedding"])) for d in items]
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected response from /embeddings") from exc


class OllamaProvider(_HttpProvider):
    """Ollama's native API (``/api/chat`` and ``/api/embed``)."""

    name = "ollama"
    label = "Ollama"

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body: dict[str, Any] = {
            "model": model or self._chat_model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            body["options"] = {"temperature": temperature}
        data = self._post("/api/chat", body)
        try:
            return (data["message"]["content"] or "").strip()
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected response from /api/chat") from exc

    def _embed_raw(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self._embedding_model, "input": texts})
        try:
            return [list(map(float, v)) for v in data["embeddings"]]
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected response from /api/embed") from exc


OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_CHAT_MODEL = "gpt-4o-mini"
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_DIMENSION = 1536
AZURE_API_VERSION = "2024-10-21"


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI's hosted API. Same wire format as any OpenAI-compatible server, fixed address."""

    name = "openai"
    label = "OpenAI"

    def __init__(
        self,
        api_key: str,
        chat_model: str = OPENAI_CHAT_MODEL,
        embedding_model: str = OPENAI_EMBEDDING_MODEL,
        dimension: int | None = OPENAI_DIMENSION,
        base_url: str = OPENAI_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        if not (api_key or "").strip():
            raise ProviderNotConfigured("OpenAI: the API key is not set.")
        super().__init__(
            base_url=base_url or OPENAI_BASE_URL,
            chat_model=chat_model or OPENAI_CHAT_MODEL,
            embedding_model=embedding_model or OPENAI_EMBEDDING_MODEL,
            api_key=api_key,
            dimension=dimension,
            transport=transport,
        )


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure OpenAI / Azure AI Foundry. Models are addressed by *deployment name* and the key is
    sent as an ``api-key`` header, so requests stay inside your Azure tenant."""

    name = "azure"
    label = "Azure AI Foundry"

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        chat_deployment: str,
        embedding_deployment: str,
        api_version: str = AZURE_API_VERSION,
        dimension: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        if not (api_key or "").strip():
            raise ProviderNotConfigured("Azure AI Foundry: the API key is not set.")
        super().__init__(
            base_url=endpoint,
            chat_model=chat_deployment,
            embedding_model=embedding_deployment,
            api_key=api_key,
            dimension=dimension,
            transport=transport,
        )
        self._api_version = (api_version or AZURE_API_VERSION).strip()

    @staticmethod
    def _normalise_base(url: str) -> str:
        return url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "api-key": self._api_key}

    def _path(self, deployment: str, op: str) -> str:
        return f"/openai/deployments/{deployment}/{op}?api-version={self._api_version}"

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body: dict[str, Any] = {"messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        data = self._post(self._path(model or self._chat_model, "chat/completions"), body)
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Unexpected response from chat/completions") from exc

    def _embed_raw(self, texts: list[str]) -> list[list[float]]:
        data = self._post(self._path(self._embedding_model, "embeddings"), {"input": texts})
        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [list(map(float, d["embedding"])) for d in items]
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected response from embeddings") from exc
