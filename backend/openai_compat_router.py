from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import ai_service
from auth import get_current_user, get_user_accessible_workspaces
from database import get_db
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from models import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.ai.openai_compat")

router = APIRouter()


class OpenAIChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] | None = ""
    name: str | None = None
    sql: str | None = None

    def text_content(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts: list[str] = []
            for item in self.content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(str(item["text"]))
                    elif "content" in item and item["content"]:
                        parts.append(str(item["content"]))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return str(self.content)


class OpenAIChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "lot-rag"
    messages: list[OpenAIChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    workspace_slug: str | None = None
    catalog_filter: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)


class OpenAIEmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    input: str | list[str]
    model: str = "text-embedding-004"
    encoding_format: str | None = "float"


def _parse_virtual_model(raw_model: str | None) -> tuple[str, str | None, str | None]:
    """Resolve (mode, workspace_slug, llm_model_override) from an OpenAI model identifier.

    Supported virtual models:
    - lot-rag / lot-rag:<workspace_slug>
    - lot-genie / lot-genie:<workspace_slug>
    - lot-hybrid / lot-hybrid:<workspace_slug>
    """
    m = (raw_model or "lot-rag").strip()
    if not m:
        return "rag", None, None

    lower_m = m.lower()
    for prefix, mode in (
        ("lot-rag", "rag"),
        ("lot-genie", "genie"),
        ("lot-hybrid", "hybrid"),
    ):
        if lower_m == prefix:
            return mode, None, None
        if lower_m.startswith(f"{prefix}:"):
            ws_part = m.split(":", 1)[1].strip()
            return mode, (ws_part or None), None

    return "rag", None, m


def _build_models_for_user(db: Session, user: User) -> list[dict[str, Any]]:
    base_created = 1700000000
    models_data: list[dict[str, Any]] = [
        {
            "id": "lot-rag",
            "object": "model",
            "created": base_created,
            "owned_by": "lake-of-tears",
            "description": "Semantic Vector RAG across all workspaces and catalogs accessible to the authenticated user.",
            "workspace_slug": None,
        },
        {
            "id": "lot-genie",
            "object": "model",
            "created": base_created,
            "owned_by": "lake-of-tears",
            "description": "Conversational Text-to-SQL + DuckDB analytics + narrative insights.",
            "workspace_slug": None,
        },
        {
            "id": "lot-hybrid",
            "object": "model",
            "created": base_created,
            "owned_by": "lake-of-tears",
            "description": "Auto-routing between DuckDB SQL analytics and Semantic Vector RAG.",
            "workspace_slug": None,
        },
    ]

    accessible_workspaces = get_user_accessible_workspaces(db, user)
    for ws in accessible_workspaces:
        ws_created = (
            int(ws.created_at.timestamp())
            if getattr(ws, "created_at", None) is not None
            else base_created
        )
        models_data.append(
            {
                "id": f"lot-rag:{ws.slug}",
                "object": "model",
                "created": ws_created,
                "owned_by": f"lake-of-tears:{ws.slug}",
                "description": f"Semantic Vector RAG scoped to workspace '{ws.name}' ({ws.slug}).",
                "workspace_slug": ws.slug,
            }
        )
        models_data.append(
            {
                "id": f"lot-genie:{ws.slug}",
                "object": "model",
                "created": ws_created,
                "owned_by": f"lake-of-tears:{ws.slug}",
                "description": f"Conversational Text-to-SQL and DuckDB analytics scoped to workspace '{ws.name}' ({ws.slug}).",
                "workspace_slug": ws.slug,
            }
        )

    return models_data


@router.get("/models")
def list_openai_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return an OpenAI-formatted model list tailored to the caller's accessible workspaces."""
    models_data = _build_models_for_user(db, current_user)
    return {
        "object": "list",
        "data": models_data,
    }


@router.get("/models/{model_id:path}")
def get_openai_model(
    model_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Retrieve a single OpenAI model descriptor if accessible to the caller."""
    models_data = _build_models_for_user(db, current_user)
    for m in models_data:
        if m["id"] == model_id:
            return m
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Model '{model_id}' not found or not accessible.",
    )


@router.post("/chat/completions")
def create_chat_completion(
    req: OpenAIChatCompletionRequest,
    x_workspace_slug: str | None = Header(default=None, alias="X-Workspace-Slug"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """OpenAI-compatible chat completions endpoint backed by Lake of Tears RAG & Genie with RBAC."""
    if not req.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="messages array cannot be empty.",
        )

    # Extract latest user question and prior conversation turns
    user_idx = -1
    for idx in range(len(req.messages) - 1, -1, -1):
        if req.messages[idx].role == "user":
            user_idx = idx
            break
    if user_idx == -1:
        user_idx = len(req.messages) - 1

    question = req.messages[user_idx].text_content().strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Latest user message content cannot be empty.",
        )

    history: list[dict[str, Any]] = []
    for msg in req.messages[:user_idx]:
        txt = msg.text_content().strip()
        if not txt:
            continue
        if msg.role in ("user", "assistant", "system"):
            history.append(
                {
                    "role": "user" if msg.role == "system" else msg.role,
                    "content": txt,
                    "sql": msg.sql,
                }
            )

    mode, model_ws_slug, llm_model = _parse_virtual_model(req.model)
    ws_Header = x_workspace_slug if isinstance(x_workspace_slug, str) else None
    target_ws_slug = model_ws_slug or req.workspace_slug or ws_Header

    scope = ai_service.resolve_user_ai_scope(
        db=db,
        user=current_user,
        workspace_slug=target_ws_slug,
        catalog_filter=req.catalog_filter,
    )
    primary_ws = scope["primary_workspace"]
    catalogs = scope["catalogs"]
    allowed_patterns = scope["allowed_patterns"]
    is_superadmin = scope["is_superadmin"]
    include_ws_pattern = not bool(req.catalog_filter and req.catalog_filter.strip())

    result = ai_service.run_genie_chat(
        question=question,
        mode=mode,
        history=history,
        catalogs=catalogs,
        model=llm_model,
        workspace_slug=primary_ws.slug,
        allowed_patterns=allowed_patterns,
        is_superadmin=is_superadmin,
        top_k=req.top_k,
        include_workspace_pattern=include_ws_pattern,
    )

    if not result.get("ok"):
        err_msg = result.get("error") or "AI completion failed."
        if result.get("error_code") == 403:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err_msg)

    answer = result.get("answer") or ""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_ts = int(time.time())

    lot_metadata = {
        "mode": mode,
        "workspace_slug": primary_ws.slug,
        "workspaces_searched": [w.slug for w in scope["target_workspaces"]],
        "catalogs": [c.get("slug") for c in catalogs if c.get("slug")],
        "snippets": result.get("snippets") or [],
        "sql": result.get("sql"),
        "row_count": result.get("row_count", 0),
        "tables_used": result.get("tables_used") or [],
    }

    prompt_tokens = max(1, len(question.split()))
    completion_tokens = max(1, len(answer.split()))

    if not req.stream:
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_ts,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": answer,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "lot_metadata": lot_metadata,
        }

    def _stream_sse():
        first_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(first_chunk)}\n\n"

        # Emit content in chunks
        chunk_size = 64
        for i in range(0, max(1, len(answer)), chunk_size):
            piece = answer[i : i + chunk_size]
            if not piece:
                continue
            chunk_obj = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": piece},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk_obj)}\n\n"

        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
            "lot_metadata": lot_metadata,
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _stream_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/embeddings")
def create_embeddings(
    req: OpenAIEmbeddingRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """OpenAI-compatible embeddings endpoint proxying to the active Lake of Tears AIProvider."""
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    if not texts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="input cannot be empty.",
        )

    effective_key = ai_service._resolve_api_key()
    if not effective_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ai_service.GEMINI_KEY_NOT_CONFIGURED_MSG,
        )

    try:
        client = ai_service._client_for(effective_key)
        embed_resp = client.models.embed_content(model=req.model, contents=texts)
        data = [
            {
                "object": "embedding",
                "index": idx,
                "embedding": list(emb.values),
            }
            for idx, emb in enumerate(embed_resp.embeddings)
        ]
        resolved_model = getattr(embed_resp, "model", None) or req.model
        token_est = sum(max(1, len(str(t).split())) for t in texts)
        return {
            "object": "list",
            "data": data,
            "model": resolved_model,
            "usage": {
                "prompt_tokens": token_est,
                "total_tokens": token_est,
            },
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Embedding generation failed: {exc}",
        ) from exc
