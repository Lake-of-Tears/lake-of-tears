from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx

logger = logging.getLogger("lake.backend.notebooks")

JUPYTER_SERVER_URL = os.getenv("JUPYTER_SERVER_URL", "http://jupyter:8888").rstrip("/")
JUPYTER_BASE_URL = os.getenv("JUPYTER_BASE_URL", "/jupyter").strip("/")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")

IGNORED_WORKSPACE_DIRS: set[str] = {
    "__pycache__",
    ".ipynb_checkpoints",
    "node_modules",
    ".git",
    "data",
    "raw",
    "scratch",
    "tmp",
    ".tmp",
    ".cache",
    ".duckdb",
}

IGNORED_WORKSPACE_FILE_EXTENSIONS: set[str] = {
    ".parquet",
    ".arrow",
    ".feather",
    ".csv",
    ".tsv",
    ".db",
    ".duckdb",
    ".sqlite",
    ".sqlite3",
    ".bin",
    ".tar",
    ".gz",
    ".zip",
    ".pkl",
    ".pickle",
    ".h5",
    ".hdf5",
}


def get_jupyter_server_url() -> str:
    """Return the base Jupyter Server URL, checking environment dynamically."""
    return os.getenv("JUPYTER_SERVER_URL", JUPYTER_SERVER_URL).rstrip("/")


def get_jupyter_base_url() -> str:
    """Return the base URL path prefix for Jupyter Server."""
    return os.getenv("JUPYTER_BASE_URL", JUPYTER_BASE_URL).strip("/")


def get_jupyter_token() -> str:
    """Return the configured Jupyter token dynamically."""
    return os.getenv("JUPYTER_TOKEN", JUPYTER_TOKEN)


def get_jupyter_api_base() -> str:
    """Return the absolute URL to Jupyter Server's API root."""
    server = get_jupyter_server_url()
    base = get_jupyter_base_url()
    if base:
        return f"{server}/{base}"
    return server


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
    token = get_jupyter_token()
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


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


async def list_workspace_notebooks(workspace_slug: str, subpath: str = "") -> dict[str, Any]:
    """List notebooks and folders in the workspace directory."""
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
            name = entry.get("name", "")
            if name.startswith(".") or name in IGNORED_WORKSPACE_DIRS:
                continue
            entry_type = entry.get("type")
            if entry_type == "file" and any(
                name.lower().endswith(ext) for ext in IGNORED_WORKSPACE_FILE_EXTENSIONS
            ):
                continue
            raw_path = entry.get("path", "")
            prefix = get_workspace_dir(workspace_slug) + "/"
            rel_path = raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path
            items.append(
                {
                    "name": name,
                    "path": rel_path,
                    "type": entry_type,
                    "last_modified": entry.get("last_modified"),
                    "created": entry.get("created"),
                    "size": entry.get("size"),
                    "writable": entry.get("writable", True),
                }
            )
    return {"items": items, "path": subpath, "workspace": workspace_slug}


async def get_notebook(workspace_slug: str, path: str) -> tuple[int, dict[str, Any]]:
    """Retrieve a notebook by path within the workspace."""
    ws_path = get_workspace_dir(workspace_slug, path)
    status, data = await jupyter_request("GET", f"contents/{ws_path}", params={"type": "notebook"})
    if status == 200 and isinstance(data, dict):
        return 200, data
    return status, {"error": "Notebook not found" if status == 404 else data}


async def save_notebook(
    workspace_slug: str, path: str, notebook_data: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Save/update notebook content."""
    ws_path = get_workspace_dir(workspace_slug, path)
    payload = {
        "type": "notebook",
        "format": "json",
        "content": notebook_data.get("content", notebook_data),
    }
    status, data = await jupyter_request("PUT", f"contents/{ws_path}", json_data=payload)
    return status, data if isinstance(data, dict) else {"result": data}


async def create_notebook(
    workspace_slug: str, folder_path: str = "", name: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Create a new notebook inside the workspace."""
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
        prefix = get_workspace_dir(workspace_slug) + "/"
        raw_path = data.get("path", "")
        data["rel_path"] = raw_path[len(prefix) :] if raw_path.startswith(prefix) else raw_path
    return status, data if isinstance(data, dict) else {"result": data}


async def create_folder(workspace_slug: str, folder_path: str) -> tuple[int, dict[str, Any]]:
    """Create a new directory inside the workspace."""
    await ensure_workspace_dir(workspace_slug)
    clean_path = sanitize_notebook_path(folder_path)
    if not clean_path:
        return 400, {"error": "Invalid folder path"}
    target_ws = get_workspace_dir(workspace_slug, clean_path)
    payload = {"type": "directory"}
    status, data = await jupyter_request("PUT", f"contents/{target_ws}", json_data=payload)
    return status, data if isinstance(data, dict) else {"result": data}


async def list_workspace_folders(
    workspace_slug: str, current_path: str = "", max_depth: int = 5
) -> list[str]:
    """Recursively list all subdirectories in the workspace."""
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
) -> list[dict[str, Any]]:
    """Recursively search for notebooks, files, and directories matching query starting from subpath."""
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


async def delete_item(workspace_slug: str, path: str) -> tuple[int, dict[str, Any]]:
    """Delete a notebook or directory within the workspace."""
    ws_path = get_workspace_dir(workspace_slug, path)
    status, data = await jupyter_request("DELETE", f"contents/{ws_path}")
    return status, data if isinstance(data, dict) else {"status": status}


async def rename_item(
    workspace_slug: str, old_path: str, new_path: str
) -> tuple[int, dict[str, Any]]:
    """Rename or move a notebook within the workspace."""
    old_ws_path = get_workspace_dir(workspace_slug, old_path)
    new_ws_path = get_workspace_dir(workspace_slug, new_path)
    payload = {"path": new_ws_path}
    status, data = await jupyter_request("PATCH", f"contents/{old_ws_path}", json_data=payload)
    return status, data if isinstance(data, dict) else {"status": status}


async def get_or_create_session(
    workspace_slug: str, notebook_path: str, kernel_name: str = "python3"
) -> tuple[int, dict[str, Any]]:
    """Start or attach to an existing kernel session for a notebook."""
    ws_path = get_workspace_dir(workspace_slug, notebook_path)
    status, sessions = await jupyter_request("GET", "sessions")
    if status == 200 and isinstance(sessions, list):
        for sess in sessions:
            if sess.get("path") == ws_path and sess.get("kernel"):
                return 200, sess

    payload = {
        "path": ws_path,
        "type": "notebook",
        "name": notebook_path.split("/")[-1],
        "kernel": {"name": kernel_name},
    }
    status, data = await jupyter_request("POST", "sessions", json_data=payload)
    return status, data if isinstance(data, dict) else {"result": data}


async def restart_kernel(kernel_id: str) -> tuple[int, dict[str, Any]]:
    """Restart a running kernel."""
    status, data = await jupyter_request("POST", f"kernels/{kernel_id}/restart")
    return status, data if isinstance(data, dict) else {"status": status}


async def interrupt_kernel(kernel_id: str) -> tuple[int, dict[str, Any]]:
    """Interrupt kernel execution."""
    status, data = await jupyter_request("POST", f"kernels/{kernel_id}/interrupt")
    return status, data if isinstance(data, dict) else {"status": status}


def list_archived_runs(workspace_slug: str, notebook_path: str) -> list[dict[str, Any]]:
    """List archived historical runs from MinIO S3 for a given notebook."""
    clean_slug = sanitize_notebook_path(workspace_slug)
    clean_nb = sanitize_notebook_path(notebook_path).replace(".ipynb", "")
    prefix = f"notebook_runs/{clean_slug}/{clean_nb}/"
    results = []

    try:
        endpoint = os.getenv("MINIO_ENDPOINT", MINIO_ENDPOINT)
        access_key = os.getenv("MINIO_ACCESS_KEY", MINIO_ACCESS_KEY)
        secret_key = os.getenv("MINIO_SECRET_KEY", MINIO_SECRET_KEY)
        bucket = os.getenv("MINIO_BUCKET", MINIO_BUCKET)
        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://{endpoint}",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
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
