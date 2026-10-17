"""Pluggable LLM and embedding providers for Lake of Tears.

Everything that needs a language model or embeddings goes through an ``AIProvider`` obtained from
``get_active_provider``; credentials never leave the backend.
"""

from __future__ import annotations

from ai_providers.base import (
    AIProvider,
    EmbeddingProvider,
    LLMProvider,
    ProviderError,
    ProviderInfo,
    ProviderNotConfigured,
)
from ai_providers.registry import (
    PROVIDERS,
    build_provider,
    get_active_provider,
    load_provider_config,
    public_provider_config,
    save_provider_config,
)

__all__ = [
    "PROVIDERS",
    "AIProvider",
    "EmbeddingProvider",
    "LLMProvider",
    "ProviderError",
    "ProviderInfo",
    "ProviderNotConfigured",
    "build_provider",
    "get_active_provider",
    "load_provider_config",
    "public_provider_config",
    "save_provider_config",
]
