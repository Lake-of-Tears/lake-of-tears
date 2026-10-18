from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx

logger = logging.getLogger("lake.notebooks")

JUPYTER_SERVER_URL = os.getenv("JUPYTER_SERVER_URL", "http://jupyter:8888").rstrip("/")
JUPYTER_BASE_URL = os.getenv("JUPYTER_BASE_URL", "/jupyter").strip("/")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")

AIRFLOW_URL = os.getenv("AIRFLOW_URL", "http://airflow:8080/airflow").rstrip("/")
AIRFLOW_ADMIN_USER = os.getenv("AIRFLOW_ADMIN_USER", "admin")
AIRFLOW_ADMIN_PASSWORD = os.getenv("AIRFLOW_ADMIN_PASSWORD") or "airflowadminpassword"

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000").rstrip("/")
USE_BACKEND_NOTEBOOKS = os.getenv("USE_BACKEND_NOTEBOOKS", "true").lower() in ("true", "1", "yes")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")

WORKSPACES_DIR = os.getenv("WORKSPACES_DIR", "/data/workspaces")
_FS_SCAN_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_FS_SCAN_TTL_SECONDS = 30.0


def clear_fs_scan_cache(workspace_slug: str | None = None) -> None:
    """Clear in-memory filesystem scan cache."""
    if workspace_slug:
        try:
            clean_slug = sanitize_notebook_path(workspace_slug) or "default"
            _FS_SCAN_CACHE.pop(clean_slug, None)
        except Exception:
            _FS_SCAN_CACHE.clear()
    else:
        _FS_SCAN_CACHE.clear()


def scan_workspace_filesystem(
    workspace_slug: str,
    max_depth: int = 10,
    limit: int = 1000,
    force_refresh: bool = False,
) -> list[dict[str, Any]] | None:
    """Scan local workspace directory using os.scandir with 30s in-memory TTL caching.

    Returns None if WORKSPACES_DIR does not exist or workspace directory is not found,
    signaling to fall back to Jupyter HTTP API.
    """
    try:
        clean_slug = sanitize_notebook_path(workspace_slug) or "default"
    except Exception:
        clean_slug = "default"
    ws_root = os.path.join(WORKSPACES_DIR, clean_slug)
    if not os.path.isdir(ws_root):
        return None

    import time

    now = time.time()
    if not force_refresh and clean_slug in _FS_SCAN_CACHE:
        cached_time, cached_items = _FS_SCAN_CACHE[clean_slug]
        if now - cached_time < _FS_SCAN_TTL_SECONDS:
            return cached_items

    items: list[dict[str, Any]] = []

    def _walk(current_dir: str, rel_prefix: str, depth: int) -> None:
        if depth > max_depth or len(items) >= limit:
            return
        try:
            with os.scandir(current_dir) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))
                for entry in entries:
                    if len(items) >= limit:
                        break
                    name = entry.name
                    if name.startswith(".") or name in (
                        "__pycache__",
                        ".ipynb_checkpoints",
                        "node_modules",
                        ".git",
                    ):
                        continue
                    rel_path = f"{rel_prefix}/{name}" if rel_prefix else name
                    parent_dir = rel_prefix

                    if entry.is_dir(follow_symlinks=False):
                        items.append(
                            {
                                "name": name,
                                "path": rel_path,
                                "type": "directory",
                                "parent_dir": parent_dir,
                            }
                        )
                        _walk(entry.path, rel_path, depth + 1)
                    elif entry.is_file(follow_symlinks=False):
                        entry_type = "notebook" if name.endswith(".ipynb") else "file"
                        items.append(
                            {
                                "name": name,
                                "path": rel_path,
                                "type": entry_type,
                                "parent_dir": parent_dir,
                            }
                        )
        except (PermissionError, FileNotFoundError, OSError) as exc:
            logger.warning("Filesystem scan error at %s: %s", current_dir, exc)

    _walk(ws_root, "", 0)
    _FS_SCAN_CACHE[clean_slug] = (now, items)
    return items


def get_jupyter_api_base() -> str:
    """Return the absolute URL to Jupyter Server's API root."""
    if JUPYTER_BASE_URL:
        return f"{JUPYTER_SERVER_URL}/{JUPYTER_BASE_URL}"
    return JUPYTER_SERVER_URL


def sanitize_notebook_path(path: str) -> str:
    """Sanitize and validate a notebook or directory path within a workspace.

    Prevents directory traversal ('..') and invalid control characters.
    """
    clean = path.replace("\\", "/").strip("/ ")
    if not clean:
        return ""
    segments = clean.split("/")
    for segment in segments:
        if segment in (".", "..") or not segment:
            raise ValueError(f"Invalid path traversal segment: {segment}")
        if re.search(r'[\x00-\x1f<>:"|?*]', segment):
            raise ValueError(f"Invalid characters in path segment: {segment}")
    return clean


def get_workspace_dir(workspace_slug: str, subpath: str = "") -> str:
    """Return the Jupyter contents path for a given workspace and subpath."""
    clean_slug = sanitize_notebook_path(workspace_slug) or "default"
    clean_subpath = sanitize_notebook_path(subpath)
    if clean_subpath:
        return f"workspaces/{clean_slug}/{clean_subpath}"
    return f"workspaces/{clean_slug}"


def create_empty_notebook_dict(title: str = "Untitled") -> dict[str, Any]:
    """Return a standard Jupyter Notebook format v4 dictionary."""
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [f"# {title}\n", "Interactive notebook in Lake of Tears.\n"],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "# Query Lake of Tears with DuckDB\n",
                    "import duckdb\n\n",
                    "con = duckdb.connect()\n",
                    "con.execute('SELECT 1 AS status, \\'Lake of Tears\\' AS lakehouse').df()\n",
                ],
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
                "version": "3.11",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _get_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if JUPYTER_TOKEN:
        headers["Authorization"] = f"token {JUPYTER_TOKEN}"
    return headers


async def _backend_request(
    method: str,
    endpoint: str,
    workspace_slug: str = "",
    params: dict[str, Any] | None = None,
    json_data: dict[str, Any] | None = None,
    auth_token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any] | None:
    """Attempt to forward request to core backend API gateway."""
    if not USE_BACKEND_NOTEBOOKS or not BACKEND_URL:
        return None
    url = f"{BACKEND_URL}/api/v1/notebooks/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req_params = {}
    if workspace_slug:
        req_params["workspace_slug"] = workspace_slug
    if params:
        req_params.update(params)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                params=req_params or None,
                json=json_data,
            )
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return resp.status_code, data
    except Exception as exc:
        logger.debug("Backend proxy skipped or unavailable, using direct fallback: %s", exc)
        return None


async def jupyter_request(
    method: str,
    path: str,
    json_data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    """Execute an authenticated HTTP request against the internal Jupyter Server REST API."""
    url = f"{get_jupyter_api_base()}/api/{path.lstrip('/')}"
    headers = _get_headers()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
            )
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return resp.status_code, data
    except httpx.ConnectError:
        logger.warning("Could not connect to Jupyter Server at %s", url)
        return 503, {"error": "Jupyter Server connection unavailable"}
    except Exception as exc:
        logger.error("Jupyter request error: %s", exc)
        return 500, {"error": str(exc)}


async def ensure_workspace_dir(workspace_slug: str) -> None:
    """Ensure the workspaces/ and workspaces/{slug}/ directories exist in Jupyter."""
    try:
        # Ensure workspaces root
        await jupyter_request("PUT", "contents/workspaces", json_data={"type": "directory"})
    except Exception:
        pass

    clean_slug = sanitize_notebook_path(workspace_slug) or "default"
    target_path = f"workspaces/{clean_slug}"
    status, _ = await jupyter_request("GET", f"contents/{target_path}")
    if status != 200:
        await jupyter_request(
            "PUT",
            f"contents/{target_path}",
            json_data={"type": "directory"},
        )


async def list_workspace_notebooks(
    workspace_slug: str, subpath: str = "", auth_token: str | None = None
) -> dict[str, Any]:
    """List notebooks and folders in the workspace directory."""
    if auth_token:
        backend_res = await _backend_request(
            "GET", "files", workspace_slug, params={"subpath": subpath}, auth_token=auth_token
        )
        if backend_res is not None:
            status, data = backend_res
            if status == 200 and isinstance(data, dict):
                return data

    await ensure_workspace_dir(workspace_slug)
    ws_path = get_workspace_dir(workspace_slug, subpath)
    status, data = await jupyter_request("GET", f"contents/{ws_path}")
    if status != 200:
        return {
            "items": [],
            "path": subpath,
            "error": data if isinstance(data, dict) else str(data),
        }

    items = []
    if isinstance(data, dict) and "content" in data and isinstance(data["content"], list):
        for entry in data["content"]:
            # Extract relative path to workspace root
            raw_path = entry.get("path", "")
            prefix = get_workspace_dir(workspace_slug) + "/"
            rel_path = raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path
            items.append(
                {
                    "name": entry.get("name"),
                    "path": rel_path,
                    "type": entry.get("type"),
                    "last_modified": entry.get("last_modified"),
                    "created": entry.get("created"),
                    "size": entry.get("size"),
                    "writable": entry.get("writable", True),
                }
            )
    return {"items": items, "path": subpath, "workspace": workspace_slug}


async def get_notebook(
    workspace_slug: str, path: str, auth_token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Retrieve a notebook by path within the workspace."""
    if auth_token:
        clean_path = sanitize_notebook_path(path)
        backend_res = await _backend_request(
            "GET", clean_path, workspace_slug, auth_token=auth_token
        )
        if backend_res is not None:
            return backend_res

    ws_path = get_workspace_dir(workspace_slug, path)
    status, data = await jupyter_request("GET", f"contents/{ws_path}", params={"type": "notebook"})
    if status == 200 and isinstance(data, dict):
        return 200, data
    return status, {"error": "Notebook not found" if status == 404 else data}


async def save_notebook(
    workspace_slug: str,
    path: str,
    notebook_data: dict[str, Any],
    auth_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Save/update notebook content."""
    if auth_token:
        clean_path = sanitize_notebook_path(path)
        backend_res = await _backend_request(
            "PUT", clean_path, workspace_slug, json_data=notebook_data, auth_token=auth_token
        )
        if backend_res is not None:
            return backend_res

    ws_path = get_workspace_dir(workspace_slug, path)
    payload = {
        "type": "notebook",
        "format": "json",
        "content": notebook_data.get("content", notebook_data),
    }
    status, data = await jupyter_request("PUT", f"contents/{ws_path}", json_data=payload)
    return status, data if isinstance(data, dict) else {"result": data}


async def create_notebook(
    workspace_slug: str,
    folder_path: str = "",
    name: str | None = None,
    auth_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Create a new notebook inside the workspace."""
    if auth_token:
        backend_res = await _backend_request(
            "POST",
            "create",
            workspace_slug,
            json_data={"type": "notebook", "folder": folder_path, "name": name},
            auth_token=auth_token,
        )
        if backend_res is not None:
            return backend_res

    await ensure_workspace_dir(workspace_slug)
    clean_folder = sanitize_notebook_path(folder_path) if folder_path else ""
    if clean_folder and name and name.startswith(clean_folder + "/"):
        name = name[len(clean_folder) + 1 :]
    ws_dir = get_workspace_dir(workspace_slug, clean_folder)
    base_name = name or f"notebook_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.ipynb"
    if not base_name.endswith(".ipynb"):
        base_name += ".ipynb"

    target_path = f"{ws_dir}/{base_name}"
    nb_dict = create_empty_notebook_dict(
        title=base_name.replace(".ipynb", "").replace("_", " ").title()
    )
    payload = {
        "type": "notebook",
        "format": "json",
        "content": nb_dict,
    }
    status, data = await jupyter_request("PUT", f"contents/{target_path}", json_data=payload)
    if status in (200, 201) and isinstance(data, dict):
        clear_fs_scan_cache(workspace_slug)
        prefix = get_workspace_dir(workspace_slug) + "/"
        raw_path = data.get("path", "")
        data["rel_path"] = raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path
    return status, data if isinstance(data, dict) else {"result": data}


async def create_folder(
    workspace_slug: str, folder_path: str, auth_token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Create a new directory inside the workspace."""
    if auth_token:
        backend_res = await _backend_request(
            "POST",
            "create",
            workspace_slug,
            json_data={"type": "directory", "name": folder_path},
            auth_token=auth_token,
        )
        if backend_res is not None:
            clear_fs_scan_cache(workspace_slug)
            return backend_res

    await ensure_workspace_dir(workspace_slug)
    clean_path = sanitize_notebook_path(folder_path)
    if not clean_path:
        return 400, {"error": "Invalid folder path"}
    target_ws = get_workspace_dir(workspace_slug, clean_path)
    payload = {"type": "directory"}
    status, data = await jupyter_request("PUT", f"contents/{target_ws}", json_data=payload)
    if status in (200, 201):
        clear_fs_scan_cache(workspace_slug)
    return status, data if isinstance(data, dict) else {"result": data}


async def list_workspace_folders(
    workspace_slug: str,
    current_path: str = "",
    max_depth: int = 5,
    auth_token: str | None = None,
) -> list[str]:
    """Recursively list all subdirectories in the workspace."""
    if auth_token:
        backend_res = await _backend_request(
            "GET", "directories", workspace_slug, auth_token=auth_token
        )
        if backend_res is not None:
            status, data = backend_res
            if status == 200 and isinstance(data, dict) and "directories" in data:
                return data["directories"]

    await ensure_workspace_dir(workspace_slug)
    folders: list[str] = []
    queue: list[tuple[str, int]] = [(current_path, 0)]
    while queue:
        sub, depth = queue.pop(0)
        if depth > max_depth:
            continue
        ws_path = get_workspace_dir(workspace_slug, sub)
        status, data = await jupyter_request("GET", f"contents/{ws_path}")
        if status != 200 or not isinstance(data, dict):
            continue
        content = data.get("content", [])
        if not isinstance(content, list):
            continue
        prefix = get_workspace_dir(workspace_slug) + "/"
        for entry in content:
            if entry.get("type") == "directory":
                raw_path = entry.get("path", "")
                rel_path = raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path
                folders.append(rel_path)
                queue.append((rel_path, depth + 1))
    return sorted(folders)


async def search_workspace_items(
    workspace_slug: str,
    query: str = "",
    subpath: str = "",
    max_depth: int = 10,
    limit: int = 300,
    auth_token: str | None = None,
) -> list[dict[str, Any]]:
    """Recursively search for notebooks, files, and directories matching query starting from subpath."""
    if auth_token:
        backend_res = await _backend_request(
            "GET",
            "search",
            workspace_slug,
            params={"q": query, "subpath": subpath},
            auth_token=auth_token,
        )
        if backend_res is not None:
            status, data = backend_res
            if status == 200 and isinstance(data, dict) and "items" in data:
                return data["items"]

    # Fast path: check local filesystem scanner first if workspace directory exists on disk
    fs_items = scan_workspace_filesystem(workspace_slug, max_depth=max_depth, limit=limit)
    if fs_items is not None:
        clean_subpath = sanitize_notebook_path(subpath) if subpath else ""
        q = query.strip().lower()
        results: list[dict[str, Any]] = []
        for item in fs_items:
            path = item["path"]
            if clean_subpath and not (
                path == clean_subpath or path.startswith(clean_subpath + "/")
            ):
                continue
            matches = True
            if q:
                if "/" in q:
                    matches = q in path.lower()
                else:
                    matches = q in item["name"].lower()
            if matches:
                results.append(item)
                if len(results) >= limit:
                    break
        results.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"].lower()))
        return results

    await ensure_workspace_dir(workspace_slug)
    clean_subpath = sanitize_notebook_path(subpath) if subpath else ""
    q = query.strip().lower()

    results: list[dict[str, Any]] = []
    queue: list[tuple[str, int]] = [(clean_subpath, 0)]
    visited: set[str] = set()

    prefix = get_workspace_dir(workspace_slug) + "/"

    while queue and len(results) < limit:
        curr, depth = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)

        if depth > max_depth:
            continue

        ws_path = get_workspace_dir(workspace_slug, curr)
        status, data = await jupyter_request("GET", f"contents/{ws_path}")
        if status != 200 or not isinstance(data, dict):
            continue

        content = data.get("content", [])
        if not isinstance(content, list):
            continue

        for entry in content:
            raw_path = entry.get("path", "")
            rel_path = raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path
            if not rel_path:
                continue

            name = entry.get("name") or rel_path.split("/")[-1]
            entry_type = entry.get("type", "file")
            parent_dir = "/".join(rel_path.split("/")[:-1]) if "/" in rel_path else ""

            matches = True
            if q:
                if "/" in q:
                    matches = q in rel_path.lower()
                else:
                    matches = q in name.lower()

            if matches:
                results.append(
                    {
                        "name": name,
                        "path": rel_path,
                        "type": entry_type,
                        "parent_dir": parent_dir,
                        "last_modified": entry.get("last_modified"),
                        "created": entry.get("created"),
                        "size": entry.get("size"),
                        "writable": entry.get("writable", True),
                    }
                )
                if len(results) >= limit:
                    break

            if entry_type == "directory":
                queue.append((rel_path, depth + 1))

    results.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"].lower()))
    return results


async def delete_item(
    workspace_slug: str, path: str, auth_token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Delete a notebook or directory within the workspace."""
    if auth_token:
        clean_path = sanitize_notebook_path(path)
        backend_res = await _backend_request(
            "DELETE", clean_path, workspace_slug, auth_token=auth_token
        )
        if backend_res is not None:
            clear_fs_scan_cache(workspace_slug)
            return backend_res

    ws_path = get_workspace_dir(workspace_slug, path)
    status, data = await jupyter_request("DELETE", f"contents/{ws_path}")
    if status in (200, 204):
        clear_fs_scan_cache(workspace_slug)
    return status, data if isinstance(data, dict) else {"status": status}


async def rename_item(
    workspace_slug: str, old_path: str, new_path: str, auth_token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Rename or move a notebook within the workspace."""
    if auth_token:
        clean_old = sanitize_notebook_path(old_path)
        backend_res = await _backend_request(
            "PATCH",
            clean_old,
            workspace_slug,
            json_data={"new_path": new_path},
            auth_token=auth_token,
        )
        if backend_res is not None:
            clear_fs_scan_cache(workspace_slug)
            return backend_res

    old_ws_path = get_workspace_dir(workspace_slug, old_path)
    new_ws_path = get_workspace_dir(workspace_slug, new_path)
    payload = {"path": new_ws_path}
    status, data = await jupyter_request("PATCH", f"contents/{old_ws_path}", json_data=payload)
    if status in (200, 201):
        clear_fs_scan_cache(workspace_slug)
    return status, data if isinstance(data, dict) else {"status": status}


async def get_or_create_session(
    workspace_slug: str,
    notebook_path: str,
    kernel_name: str = "python3",
    auth_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Start or attach to an existing kernel session for a notebook."""
    if auth_token:
        backend_res = await _backend_request(
            "POST",
            "sessions",
            workspace_slug,
            json_data={"path": notebook_path, "kernel_name": kernel_name},
            auth_token=auth_token,
        )
        if backend_res is not None:
            return backend_res

    ws_path = get_workspace_dir(workspace_slug, notebook_path)
    # Check existing sessions first
    status, sessions = await jupyter_request("GET", "sessions")
    if status == 200 and isinstance(sessions, list):
        for sess in sessions:
            if sess.get("path") == ws_path and sess.get("kernel"):
                return 200, sess

    # Start new session
    payload = {
        "path": ws_path,
        "type": "notebook",
        "name": notebook_path.split("/")[-1],
        "kernel": {"name": kernel_name},
    }
    status, data = await jupyter_request("POST", "sessions", json_data=payload)
    return status, data if isinstance(data, dict) else {"result": data}


async def restart_kernel(
    kernel_id: str, auth_token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Restart a running kernel."""
    if auth_token:
        backend_res = await _backend_request(
            "POST", f"kernels/{kernel_id}/restart", auth_token=auth_token
        )
        if backend_res is not None:
            return backend_res

    status, data = await jupyter_request("POST", f"kernels/{kernel_id}/restart")
    return status, data if isinstance(data, dict) else {"status": status}


async def interrupt_kernel(
    kernel_id: str, auth_token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Interrupt kernel execution."""
    if auth_token:
        backend_res = await _backend_request(
            "POST", f"kernels/{kernel_id}/interrupt", auth_token=auth_token
        )
        if backend_res is not None:
            return backend_res

    status, data = await jupyter_request("POST", f"kernels/{kernel_id}/interrupt")
    return status, data if isinstance(data, dict) else {"status": status}


async def trigger_airflow_notebook_run(
    workspace_slug: str,
    notebook_path: str,
    parameters: dict[str, Any] | None = None,
    user_email: str = "system",
) -> tuple[int, dict[str, Any]]:
    """Trigger on-demand execution of a notebook via Airflow REST API."""
    url = f"{AIRFLOW_URL}/api/v1/dags/run_notebook/dagRuns"
    clean_nb = re.sub(r"[^a-zA-Z0-9._-]", "_", sanitize_notebook_path(notebook_path))
    clean_user = re.sub(r"[^a-zA-Z0-9._-]", "_", user_email)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"manual__{ts}__{clean_user}__{clean_nb}"
    payload = {
        "dag_run_id": run_id,
        "conf": {
            "workspace_slug": workspace_slug,
            "notebook_path": notebook_path,
            "parameters": parameters or {},
            "triggered_by": user_email,
        },
    }
    auth = (AIRFLOW_ADMIN_USER, AIRFLOW_ADMIN_PASSWORD) if AIRFLOW_ADMIN_PASSWORD else None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if auth:
                try:
                    await client.patch(
                        f"{AIRFLOW_URL}/api/v1/dags/run_notebook",
                        json={"is_paused": False},
                        auth=auth,
                        timeout=5.0,
                    )
                except Exception:
                    pass

            resp = await client.post(url, json=payload, auth=auth)
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return resp.status_code, data if isinstance(data, dict) else {
                "result": data,
                "dag_run_id": run_id,
            }
    except Exception as exc:
        logger.error("Failed to trigger Airflow run: %s", exc)
        return 500, {"error": str(exc), "dag_run_id": run_id}


def list_archived_runs(workspace_slug: str, notebook_path: str) -> list[dict[str, Any]]:
    """List archived historical runs from MinIO S3 for a given notebook."""
    clean_slug = sanitize_notebook_path(workspace_slug)
    clean_nb = sanitize_notebook_path(notebook_path).replace(".ipynb", "")
    prefix = f"notebook_runs/{clean_slug}/{clean_nb}/"
    results = []

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://{MINIO_ENDPOINT}",
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
            region_name="us-east-1",
        )
        resp = s3.list_objects_v2(Bucket=MINIO_BUCKET, Prefix=prefix)
        for obj in resp.get("Contents", []):
            key = obj.get("Key", "")
            filename = key.split("/")[-1]
            results.append(
                {
                    "key": key,
                    "filename": filename,
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified", datetime.now(UTC)).isoformat(),
                }
            )
    except Exception as exc:
        logger.warning("Could not list notebook run archives: %s", exc)
    return results
