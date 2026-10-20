from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import notebook_service
import websockets
from auth import decode_token, get_current_user, get_notebook_workspace_and_role
from database import get_db
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse, Response
from models import User
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.notebooks.router")

router = APIRouter()


def _resolve_workspace_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Any, str]:
    slug = workspace_slug or x_workspace_slug
    ws_id = x_workspace_id or lake_active_workspace or lake_workspace_id
    return get_notebook_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=require_write,
    )


# ── Files & Directory Contents ───────────────────────────────────────────────


@router.get("/files")
@router.get("/contents")
@router.get("")
async def list_notebooks(
    subpath: str = "",
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    res = await notebook_service.list_workspace_notebooks(ws.slug, subpath)
    return JSONResponse(res)


@router.post("/create")
@router.post("/contents")
@router.post("")
async def create_item(
    request: Request,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    try:
        body = await request.json()
    except Exception:
        body = {}
    item_type = body.get("type", "notebook")
    name = body.get("name")
    folder = body.get("folder") or body.get("folder_path") or ""
    if item_type == "directory":
        target = f"{folder}/{name}" if folder and name else (name or "new_folder")
        status_code, data = await notebook_service.create_folder(ws.slug, target)
    else:
        status_code, data = await notebook_service.create_notebook(ws.slug, folder, name)
    return JSONResponse(data, status_code=status_code)


@router.get("/directories")
async def list_directories(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    folders = await notebook_service.list_workspace_folders(ws.slug)
    return JSONResponse({"directories": folders, "workspace": ws.slug})


@router.get("/search")
async def search_notebooks(
    q: str = "",
    subpath: str = "",
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    try:
        clean_subpath = notebook_service.sanitize_notebook_path(subpath) if subpath else ""
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    items = await notebook_service.search_workspace_items(ws.slug, query=q, subpath=clean_subpath)
    return JSONResponse(
        {
            "items": items,
            "query": q,
            "subpath": clean_subpath,
            "workspace": ws.slug,
        }
    )


# ── Execution Sessions & Kernels ─────────────────────────────────────────────


@router.post("/sessions")
async def create_session(
    request: Request,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = body.get("path", "")
    kernel_name = body.get("kernel_name", "python3")
    status_code, data = await notebook_service.get_or_create_session(ws.slug, path, kernel_name)
    return JSONResponse(data, status_code=status_code)


@router.post("/kernels/{kernel_id}/interrupt")
async def interrupt_kernel(
    kernel_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    status_code, data = await notebook_service.interrupt_kernel(kernel_id)
    return JSONResponse(data, status_code=status_code)


@router.post("/kernels/{kernel_id}/restart")
async def restart_kernel(
    kernel_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    status_code, data = await notebook_service.restart_kernel(kernel_id)
    return JSONResponse(data, status_code=status_code)


# ── Run Archives ─────────────────────────────────────────────────────────────


@router.get("/runs")
async def list_runs(
    path: str = "",
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    runs = notebook_service.list_archived_runs(ws.slug, path)
    return JSONResponse({"runs": runs, "workspace": ws.slug, "notebook": path})


# ── Path-based CRUD (Get, Save, Delete, Rename/Move) ─────────────────────────


@router.get("/{path:path}")
async def get_notebook_item(
    path: str,
    download: bool = Query(False),
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    try:
        clean_path = notebook_service.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status_code, data = await notebook_service.get_notebook(ws.slug, clean_path)
    if status_code != 200:
        return JSONResponse(data, status_code=status_code)

    if download:
        filename = clean_path.split("/")[-1]
        if not filename.endswith(".ipynb"):
            filename = f"{filename}.ipynb"
        content = data.get("content", data)
        return Response(
            content=json.dumps(content, indent=2),
            media_type="application/x-ipynb+json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return JSONResponse(data, status_code=status_code)


@router.put("/{path:path}")
async def save_notebook_item(
    path: str,
    request: Request,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    try:
        clean_path = notebook_service.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    status_code, data = await notebook_service.save_notebook(ws.slug, clean_path, body)
    return JSONResponse(data, status_code=status_code)


@router.delete("/{path:path}")
async def delete_notebook_item(
    path: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    try:
        clean_path = notebook_service.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status_code, data = await notebook_service.delete_item(ws.slug, clean_path)
    return JSONResponse(data, status_code=status_code)


@router.patch("/{path:path}")
async def rename_notebook_item(
    path: str,
    request: Request,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ws, _ = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    try:
        clean_path = notebook_service.sanitize_notebook_path(path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_path = body.get("new_path")
    if not new_path:
        return JSONResponse({"error": "new_path is required"}, status_code=400)
    try:
        clean_new_path = notebook_service.sanitize_notebook_path(new_path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status_code, data = await notebook_service.rename_item(ws.slug, clean_path, clean_new_path)
    return JSONResponse(data, status_code=status_code)


# ── WebSocket Proxy for Kernel Execution ─────────────────────────────────────


@router.websocket("/kernels/{kernel_id}/channels")
async def websocket_kernel_proxy(
    websocket: WebSocket,
    kernel_id: str,
    db: Session = Depends(get_db),
):
    await websocket.accept()

    token = websocket.cookies.get("lake_token") or websocket.query_params.get("token")
    if not token and websocket.headers.get("Authorization", "").startswith("Bearer "):
        token = websocket.headers.get("Authorization", "").split(" ", 1)[1]

    if not token:
        await websocket.send_json(
            {
                "msg_type": "error",
                "content": {"ename": "AuthError", "evalue": "Authentication required"},
            }
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        payload = decode_token(token)
        sub_val = payload.get("sub")
        user_uuid = uuid.UUID(str(sub_val))
        user = db.query(User).filter(User.id == user_uuid).first()
        if not user or not user.is_active:
            raise ValueError("User inactive or not found")
    except Exception as exc:
        await websocket.send_json(
            {
                "msg_type": "error",
                "content": {"ename": "AuthError", "evalue": f"Invalid token: {exc}"},
            }
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Check workspace role (viewer cannot execute cells)
    ws_slug = websocket.query_params.get("workspace_slug")
    ws_id = websocket.cookies.get("lake_active_workspace") or websocket.query_params.get(
        "workspace_id"
    )
    try:
        ws, ws_role = get_notebook_workspace_and_role(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            current_user=user,
            db=db,
            require_write=True,
        )
    except HTTPException as exc:
        await websocket.send_json(
            {
                "msg_type": "error",
                "content": {"ename": "PermissionError", "evalue": exc.detail},
            }
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Forward to Jupyter Server WebSocket
    api_base = notebook_service.get_jupyter_api_base()
    ws_base = api_base.replace("http://", "ws://").replace("https://", "wss://")
    upstream_url = f"{ws_base}/api/kernels/{kernel_id}/channels"
    token = notebook_service.get_jupyter_token()
    if token:
        upstream_url += f"?token={token}"

    try:
        async with websockets.connect(upstream_url, proxy=None) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await upstream.send(msg)
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as exc:
                    logger.debug("Client to upstream error: %s", exc)

            async def upstream_to_client():
                try:
                    while True:
                        msg = await upstream.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as exc:
                    logger.debug("Upstream to client error: %s", exc)

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:
        logger.warning("Upstream WebSocket connection error: %s", exc)
        try:
            await websocket.send_json(
                {
                    "msg_type": "error",
                    "content": {
                        "ename": "ConnectionError",
                        "evalue": f"Cannot connect to kernel: {exc}",
                    },
                }
            )
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:
            pass
