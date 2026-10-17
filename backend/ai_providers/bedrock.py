"""Amazon Bedrock: chat through the Converse API and embeddings through InvokeModel.

Keeps prompts and documents inside your AWS account. Two ways to authenticate:

* **IAM role** (recommended on AWS): leave the access keys blank and boto3 uses the default
  credential chain: an EKS service-account role (IRSA), an EC2/ECS role, or ``AWS_*`` variables.
* **Static keys**: an access key id and secret (plus a session token for temporary credentials).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from ai_providers.base import AIProvider, ProviderError, ProviderNotConfigured

DEFAULT_CHAT_MODEL = "anthropic.claude-3-5-haiku-20241022-v1:0"
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
DEFAULT_DIMENSION = 1024
TITAN_V2_DIMENSIONS = (256, 512, 1024)

_PROBED: dict[tuple[str, str], int] = {}
_LOCK = threading.Lock()


def clear_dimension_cache() -> None:
    with _LOCK:
        _PROBED.clear()


class BedrockProvider(AIProvider):
    name = "bedrock"
    label = "Amazon Bedrock"

    def __init__(
        self,
        region: str = "",
        chat_model: str = DEFAULT_CHAT_MODEL,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        access_key_id: str = "",
        secret_access_key: str = "",
        session_token: str = "",
        dimension: int | None = None,
        client: Any = None,
    ):
        self._region = (
            region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or ""
        ).strip()
        if not self._region and client is None:
            raise ProviderNotConfigured("Amazon Bedrock: the AWS region is not set.")
        if bool(access_key_id.strip()) != bool(secret_access_key.strip()):
            raise ProviderNotConfigured(
                "Amazon Bedrock: set both the access key id and the secret access key, "
                "or leave both blank to use the IAM role."
            )
        self._chat_model = (chat_model or DEFAULT_CHAT_MODEL).strip()
        self._embedding_model = (embedding_model or DEFAULT_EMBEDDING_MODEL).strip()
        self._keys = (access_key_id.strip(), secret_access_key.strip(), session_token.strip())
        self._configured_dimension = int(dimension) if dimension else None
        self._client = client

    # -- properties -------------------------------------------------------------------
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
        key = (self._region, self._embedding_model)
        with _LOCK:
            known = _PROBED.get(key)
        if known:
            return known
        found = len(self._embed_one("dimension probe", query=False))
        with _LOCK:
            _PROBED[key] = found
        return found

    # -- boto3 ------------------------------------------------------------------------
    def _runtime(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            kwargs: dict[str, Any] = {}
            access, secret, token = self._keys
            if access:
                kwargs.update(aws_access_key_id=access, aws_secret_access_key=secret)
                if token:
                    kwargs["aws_session_token"] = token
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self._region,
                config=Config(read_timeout=180, connect_timeout=10, retries={"max_attempts": 3}),
                **kwargs,
            )
        return self._client

    def _redact(self, text: str) -> str:
        for secret in self._keys:
            if secret:
                text = text.replace(secret, "***")
        return text

    def _call(self, fn_name: str, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            NoRegionError,
        )

        try:
            return getattr(self._runtime(), fn_name)(**kwargs)
        except NoCredentialsError as exc:
            raise ProviderError(
                "Amazon Bedrock found no AWS credentials. Attach an IAM role to the workload "
                "or enter an access key id and secret access key."
            ) from exc
        except NoRegionError as exc:
            raise ProviderError("Amazon Bedrock: the AWS region is not set.") from exc
        except ClientError as exc:
            err = exc.response.get("Error", {})
            code, msg = err.get("Code", "Error"), err.get("Message", "")
            hint = ""
            if code in ("AccessDeniedException", "UnrecognizedClientException"):
                hint = " (check the credentials, the IAM permission bedrock:InvokeModel, and that model access is enabled in this region)"
            elif code == "ResourceNotFoundException" or "model identifier" in msg.lower():
                hint = " (check the model id and the region)"
            raise ProviderError(
                self._redact(f"Amazon Bedrock returned {code}: {msg}{hint}")[:500]
            ) from exc
        except BotoCoreError as exc:
            raise ProviderError(
                self._redact(f"Amazon Bedrock request failed: {exc}")[:500]
            ) from exc

    # -- chat -------------------------------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "modelId": model or self._chat_model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if temperature is not None:
            kwargs["inferenceConfig"] = {"temperature": temperature}
        data = self._call("converse", **kwargs)
        try:
            parts = data["output"]["message"]["content"]
            return "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected response from Bedrock Converse") from exc

    # -- embeddings -------------------------------------------------------------------
    def _is_cohere(self) -> bool:
        return "cohere.embed" in self._embedding_model

    def _invoke(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self._call(
            "invoke_model",
            modelId=self._embedding_model,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        raw = data["body"]
        try:
            return json.loads(raw.read() if hasattr(raw, "read") else raw)
        except (ValueError, TypeError) as exc:
            raise ProviderError("Bedrock did not return JSON for the embedding") from exc

    def _embed_one(self, text: str, *, query: bool) -> list[float]:
        return self._embed_many([text], query=query)[0]

    def _embed_many(self, texts: list[str], *, query: bool) -> list[list[float]]:
        try:
            if self._is_cohere():
                out = self._invoke(
                    {
                        "texts": texts,
                        "input_type": "search_query" if query else "search_document",
                        "embedding_types": ["float"],
                    }
                )
                emb = out["embeddings"]
                emb = emb["float"] if isinstance(emb, dict) else emb
                return [list(map(float, v)) for v in emb]
            vectors = []
            for text in texts:  # Titan embeds one text per request
                body: dict[str, Any] = {"inputText": text}
                if "titan-embed-text-v2" in self._embedding_model:
                    body["normalize"] = True
                    if self._configured_dimension in TITAN_V2_DIMENSIONS:
                        body["dimensions"] = self._configured_dimension
                vectors.append(list(map(float, self._invoke(body)["embedding"])))
            return vectors
        except (KeyError, TypeError) as exc:
            raise ProviderError("Unexpected response from the Bedrock embedding model") from exc

    def embed_texts(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._embed_many(texts, query=query)
        found = {len(v) for v in vectors}
        if len(found) != 1:
            raise ProviderError(f"{self._embedding_model} returned vectors of different lengths")
        size = found.pop()
        if self._configured_dimension and size != self._configured_dimension:
            raise ProviderError(
                f"{self._embedding_model} returned {size} dimensions, "
                f"expected {self._configured_dimension}"
            )
        if not self._configured_dimension:
            with _LOCK:
                _PROBED[(self._region, self._embedding_model)] = size
        return vectors
