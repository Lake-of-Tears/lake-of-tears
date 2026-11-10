from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class LakeOfTearsDocument:
    """LangChain & LlamaIndex compatible Document returned by LakeOfTearsRetriever."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    score: float | None = None

    @property
    def text(self) -> str:
        """LlamaIndex Node/Document compatibility alias for page_content."""
        return self.page_content

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page_content": self.page_content,
            "text": self.page_content,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


def _normalize_base_url(raw_url: str | None) -> str:
    url = (
        raw_url
        or os.environ.get("LAKE_API_URL")
        or os.environ.get("LAKE_API_BASE")
        or os.environ.get("LAKE_URL")
        or "http://localhost:8000"
    ).strip()
    url = url.rstrip("/")
    for suffix in ("/api/ai/v1", "/api/v1", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break
    return url or "http://localhost:8000"


class LakeOfTearsClient:
    """HTTP client for Lake of Tears AI Gateway, RBAC Vector Retrieval, SQL Execution, and MCP tools."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        token: str | None = None,
        workspace_slug: str | None = None,
        on_behalf_of: str | None = None,
        timeout: float = 60.0,
        http_client: Any = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_key = (
            api_key
            or token
            or os.environ.get("LAKE_API_KEY")
            or os.environ.get("LAKE_TOKEN")
            or os.environ.get("LAKE_AUTH_TOKEN")
        )
        self.workspace_slug = (
            workspace_slug
            or os.environ.get("LAKE_WORKSPACE_SLUG")
            or os.environ.get("WORKSPACE_SLUG")
        )
        self.on_behalf_of = on_behalf_of or os.environ.get("LAKE_ON_BEHALF_OF")
        self.timeout = timeout
        self._http_client = http_client

    def with_user(
        self,
        on_behalf_of: str | None = None,
        api_key: str | None = None,
        workspace_slug: str | None = None,
    ) -> LakeOfTearsClient:
        """Return a scoped clone of this client for per-user RBAC in multi-user harnesses."""
        return LakeOfTearsClient(
            base_url=self.base_url,
            api_key=api_key or self.api_key,
            workspace_slug=workspace_slug or self.workspace_slug,
            on_behalf_of=on_behalf_of if on_behalf_of is not None else self.on_behalf_of,
            timeout=self.timeout,
            http_client=self._http_client,
        )

    def _build_headers(
        self,
        api_key: str | None = None,
        workspace_slug: str | None = None,
        on_behalf_of: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        eff_key = (
            api_key
            or self.api_key
            or os.environ.get("LAKE_API_KEY")
            or os.environ.get("LAKE_TOKEN")
            or os.environ.get("LAKE_AUTH_TOKEN")
        )
        eff_ws = (
            workspace_slug
            or self.workspace_slug
            or os.environ.get("LAKE_WORKSPACE_SLUG")
            or os.environ.get("WORKSPACE_SLUG")
        )
        eff_obo = (
            on_behalf_of
            if on_behalf_of is not None
            else (self.on_behalf_of or os.environ.get("LAKE_ON_BEHALF_OF"))
        )

        if eff_key:
            headers["Authorization"] = f"Bearer {eff_key}"
        if eff_ws:
            headers["X-Workspace-Slug"] = str(eff_ws)
        if eff_obo:
            headers["X-On-Behalf-Of"] = str(eff_obo)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        api_key: str | None = None,
        workspace_slug: str | None = None,
        on_behalf_of: str | None = None,
    ) -> dict[str, Any]:
        headers = self._build_headers(
            api_key=api_key,
            workspace_slug=workspace_slug,
            on_behalf_of=on_behalf_of,
        )
        if self._http_client is not None:
            target = path if hasattr(self._http_client, "app") else f"{self.base_url}{path}"
            resp = self._http_client.request(
                method,
                target,
                json=json_body,
                params=params,
                headers=headers,
            )
        else:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    params=params,
                    headers=headers,
                )

        if resp.status_code in (401, 403):
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise PermissionError(f"Lake of Tears RBAC Error ({resp.status_code}): {detail}")

        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"Lake of Tears API Error ({resp.status_code}): {detail}")

        return resp.json() if resp.content else {}

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        workspace_slug: str | None = None,
        catalog: str | None = None,
        schema_name: str | None = None,
        table: str | None = None,
        score_threshold: float | None = None,
        on_behalf_of: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve permission-scoped semantic vector chunks via POST /api/v1/retrieve."""
        eff_ws = workspace_slug or self.workspace_slug
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
        }
        if eff_ws:
            payload["workspace_slug"] = eff_ws
        if catalog:
            payload["catalog"] = catalog
        if schema_name:
            payload["schema_name"] = schema_name
        if table:
            payload["table"] = table
        if score_threshold is not None:
            payload["score_threshold"] = score_threshold

        return self._request(
            "POST",
            "/api/v1/retrieve",
            json_body=payload,
            api_key=api_key,
            workspace_slug=eff_ws,
            on_behalf_of=on_behalf_of,
        )

    def chat_completions(
        self,
        messages: list[dict[str, str]],
        model: str = "lot-rag",
        workspace_slug: str | None = None,
        catalog_filter: str | None = None,
        top_k: int = 5,
        on_behalf_of: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Invoke the OpenAI-compatible Lake of Tears gateway (/api/v1/chat/completions)."""
        eff_ws = workspace_slug or self.workspace_slug
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "top_k": top_k,
        }
        if eff_ws:
            payload["workspace_slug"] = eff_ws
        if catalog_filter:
            payload["catalog_filter"] = catalog_filter

        return self._request(
            "POST",
            "/api/v1/chat/completions",
            json_body=payload,
            api_key=api_key,
            workspace_slug=eff_ws,
            on_behalf_of=on_behalf_of,
        )

    def execute_sql(
        self,
        sql: str,
        workspace_slug: str | None = None,
        max_rows: int = 100,
        on_behalf_of: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute a read-only DuckDB SQL query under the caller's RBAC scope."""
        eff_ws = workspace_slug or self.workspace_slug
        payload: dict[str, Any] = {
            "sql": sql,
            "read_only": True,
            "max_ai_rows": max_rows,
        }
        if eff_ws:
            payload["workspace_slug"] = eff_ws
        return self._request(
            "POST",
            "/api/ai/sql/execute",
            json_body=payload,
            api_key=api_key,
            workspace_slug=eff_ws,
            on_behalf_of=on_behalf_of,
        )

    def get_context(
        self,
        workspace_slug: str | None = None,
        on_behalf_of: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the caller's authorized catalogs, schemas, tables, and AI UDF signatures."""
        eff_ws = workspace_slug or self.workspace_slug
        params = {"workspace_slug": eff_ws} if eff_ws else None
        return self._request(
            "GET",
            "/api/ai/context",
            params=params,
            api_key=api_key,
            workspace_slug=eff_ws,
            on_behalf_of=on_behalf_of,
        )

    def call_mcp_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        on_behalf_of: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a native Lake of Tears MCP tool via POST /api/mcp."""
        rpc_resp = self._request(
            "POST",
            "/api/mcp",
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments or {},
                },
            },
            api_key=api_key,
            on_behalf_of=on_behalf_of,
        )
        if "error" in rpc_resp:
            err = rpc_resp["error"]
            raise RuntimeError(f"MCP RPC Error ({err.get('code')}): {err.get('message')}")
        result = rpc_resp.get("result") or {}
        structured = result.get("structuredContent") or {}
        if result.get("isError"):
            status_code = structured.get("status_code", 400)
            err_msg = structured.get("error", "MCP tool call failed")
            if status_code in (401, 403):
                raise PermissionError(f"Lake of Tears RBAC Error ({status_code}): {err_msg}")
            raise RuntimeError(f"Lake of Tears MCP Tool Error ({status_code}): {err_msg}")
        return structured


class LakeOfTearsRetriever:
    """LangChain & LlamaIndex compatible permission-aware retriever for Lake of Tears."""

    def __init__(
        self,
        client: LakeOfTearsClient | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        workspace_slug: str | None = None,
        catalog: str | None = None,
        schema_name: str | None = None,
        table: str | None = None,
        top_k: int = 5,
        score_threshold: float | None = None,
        on_behalf_of: str | None = None,
        http_client: Any = None,
    ) -> None:
        self.client = client or LakeOfTearsClient(
            base_url=base_url,
            api_key=api_key,
            workspace_slug=workspace_slug,
            on_behalf_of=on_behalf_of,
            http_client=http_client,
        )
        self.workspace_slug = workspace_slug or self.client.workspace_slug
        self.catalog = catalog
        self.schema_name = schema_name
        self.table = table
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.on_behalf_of = on_behalf_of or self.client.on_behalf_of

    def _extract_query_and_overrides(
        self,
        input_or_query: str | dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]]:
        if isinstance(input_or_query, dict):
            query = str(
                input_or_query.get("query")
                or input_or_query.get("question")
                or input_or_query.get("input")
                or ""
            )
        else:
            query = str(input_or_query or "")

        configurable = (config or {}).get("configurable") if isinstance(config, dict) else {}
        if not isinstance(configurable, dict):
            configurable = {}

        overrides = {
            "top_k": kwargs.get("top_k", configurable.get("top_k", self.top_k)),
            "workspace_slug": kwargs.get(
                "workspace_slug",
                configurable.get("workspace_slug", self.workspace_slug),
            ),
            "catalog": kwargs.get("catalog", configurable.get("catalog", self.catalog)),
            "schema_name": kwargs.get(
                "schema_name",
                kwargs.get("schema", configurable.get("schema_name", self.schema_name)),
            ),
            "table": kwargs.get("table", configurable.get("table", self.table)),
            "score_threshold": kwargs.get(
                "score_threshold",
                configurable.get("score_threshold", self.score_threshold),
            ),
            "on_behalf_of": kwargs.get(
                "on_behalf_of",
                configurable.get(
                    "on_behalf_of",
                    configurable.get("user_email", self.on_behalf_of),
                ),
            ),
            "api_key": kwargs.get("api_key", configurable.get("api_key", None)),
        }
        return query, overrides

    def get_relevant_documents(
        self,
        query: str,
        **kwargs: Any,
    ) -> list[LakeOfTearsDocument]:
        """Retrieve relevant documents scoped to the caller's Lake of Tears RBAC permissions."""
        clean_query, opts = self._extract_query_and_overrides(query, **kwargs)
        resp = self.client.retrieve(
            query=clean_query,
            top_k=opts["top_k"],
            workspace_slug=opts["workspace_slug"],
            catalog=opts["catalog"],
            schema_name=opts["schema_name"],
            table=opts["table"],
            score_threshold=opts["score_threshold"],
            on_behalf_of=opts["on_behalf_of"],
            api_key=opts["api_key"],
        )
        docs: list[LakeOfTearsDocument] = []
        for item in resp.get("results", []):
            raw_meta = dict(item.get("metadata") or {})
            metadata = {
                "source": item.get("source") or raw_meta.get("source"),
                "score": item.get("score"),
                "workspace_slug": raw_meta.get("workspace_slug"),
                "catalog": raw_meta.get("catalog_slug") or raw_meta.get("catalog"),
                "catalog_slug": raw_meta.get("catalog_slug") or raw_meta.get("catalog"),
                "schema": raw_meta.get("schema_slug") or raw_meta.get("schema"),
                "schema_slug": raw_meta.get("schema_slug") or raw_meta.get("schema"),
                "table": raw_meta.get("table_slug") or raw_meta.get("table"),
                "table_slug": raw_meta.get("table_slug") or raw_meta.get("table"),
                "ingested_at": item.get("ingested_at") or raw_meta.get("ingested_at"),
            }
            docs.append(
                LakeOfTearsDocument(
                    id=item.get("id"),
                    page_content=str(item.get("page_content") or item.get("text") or ""),
                    score=float(item.get("score", 0.0)),
                    metadata=metadata,
                )
            )
        return docs

    def invoke(
        self,
        input: str | dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[LakeOfTearsDocument]:
        """LangChain Runnable interface (`retriever.invoke(query, config=...)`)."""
        clean_query, opts = self._extract_query_and_overrides(input, config=config, **kwargs)
        return self.get_relevant_documents(clean_query, **opts)

    def retrieve(
        self,
        query: str,
        **kwargs: Any,
    ) -> list[LakeOfTearsDocument]:
        """LlamaIndex BaseRetriever interface (`retriever.retrieve(query)`)."""
        return self.get_relevant_documents(query, **kwargs)

    async def aretrieve(
        self,
        query: str,
        **kwargs: Any,
    ) -> list[LakeOfTearsDocument]:
        """Async retrieval for LlamaIndex / LangChain async pipelines."""
        return await asyncio.to_thread(self.get_relevant_documents, query, **kwargs)

    async def ainvoke(
        self,
        input: str | dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[LakeOfTearsDocument]:
        """Async LangChain Runnable interface (`await retriever.ainvoke(query)`)."""
        return await asyncio.to_thread(self.invoke, input, config=config, **kwargs)


class LakeOfTearsToolkit:
    """Ready-to-bind tool functions for LangGraph, CrewAI, and OpenAI/Anthropic tool-calling agents."""

    def __init__(
        self,
        client: LakeOfTearsClient | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        workspace_slug: str | None = None,
        on_behalf_of: str | None = None,
        http_client: Any = None,
    ) -> None:
        self.client = client or LakeOfTearsClient(
            base_url=base_url,
            api_key=api_key,
            workspace_slug=workspace_slug,
            on_behalf_of=on_behalf_of,
            http_client=http_client,
        )

    def search_datalake(
        self,
        query: str,
        top_k: int = 5,
        workspace_slug: str | None = None,
        catalog: str | None = None,
        table: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search authorized Lake of Tears vector embeddings and return matching chunks."""
        resp = self.client.retrieve(
            query=query,
            top_k=top_k,
            workspace_slug=workspace_slug,
            catalog=catalog,
            table=table,
        )
        return resp.get("results", [])

    def query_sql(
        self,
        sql: str,
        workspace_slug: str | None = None,
        max_rows: int = 100,
    ) -> dict[str, Any]:
        """Execute a read-only DuckDB SQL query against authorized Lake of Tears catalog tables."""
        args: dict[str, Any] = {"sql": sql, "max_rows": max_rows}
        if workspace_slug or self.client.workspace_slug:
            args["workspace_slug"] = workspace_slug or self.client.workspace_slug
        return self.client.call_mcp_tool("query_datalake_sql", args)

    def list_tables(
        self,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """List all workspaces, catalogs, schemas, and tables accessible to the current user."""
        args: dict[str, Any] = {}
        if workspace_slug or self.client.workspace_slug:
            args["workspace_slug"] = workspace_slug or self.client.workspace_slug
        return self.client.call_mcp_tool("list_workspaces_and_catalogs", args)

    def ask_genie(
        self,
        question: str,
        mode: str = "genie",
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """Ask the Lake of Tears Genie AI analyst a natural language question."""
        args: dict[str, Any] = {"question": question, "mode": mode}
        if workspace_slug or self.client.workspace_slug:
            args["workspace_slug"] = workspace_slug or self.client.workspace_slug
        return self.client.call_mcp_tool("ask_genie", args)

    def get_tools(self) -> list[Callable[..., Any]]:
        """Return bound Python callables ready for LangGraph / CrewAI / OpenAI Agents SDK."""
        return [
            self.search_datalake,
            self.query_sql,
            self.list_tables,
            self.ask_genie,
        ]
