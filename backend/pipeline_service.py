from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import uuid
import zoneinfo
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from croniter import croniter
from fastapi import HTTPException

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.pipelines")

AIRFLOW_URL = os.getenv("AIRFLOW_URL", "http://airflow:8080/airflow").rstrip("/")
AIRFLOW_ADMIN_USER = os.getenv("AIRFLOW_ADMIN_USER", "admin")
AIRFLOW_ADMIN_PASSWORD = (
    os.getenv("AIRFLOW_ADMIN_PASSWORD") or os.getenv("AIRFLOW_PASSWORD") or "admin"
)
AIRFLOW_DAGS_DIR = os.getenv("AIRFLOW_DAGS_DIR", "/opt/airflow/dags")
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "docker")

DEFAULT_PALETTE = [
    "#10B981",  # Emerald
    "#3B82F6",  # Blue
    "#6366F1",  # Indigo
    "#8B5CF6",  # Violet
    "#D946EF",  # Fuchsia
    "#EC4899",  # Pink
    "#F43F5E",  # Rose
    "#F59E0B",  # Amber
    "#F97316",  # Orange
    "#14B8A6",  # Teal
    "#06B6D4",  # Cyan
    "#84CC16",  # Lime
]

PRESET_MAP = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

KNOWN_PIPELINE_NOTEBOOKS: dict[str, str] = {
    "ingest_tmdb_movies": "tmdb_daily_movies_ingest.ipynb",
}

PROTECTED_SYSTEM_DAGS: set[str] = {
    "run_notebook",
    "refresh_catalog_schemas_dag",
    "embed_sources_dag",
    "summarize_dag",
    "anomaly_detect_dag",
    "generate_embeddings_dag",
}


def get_default_color_for_dag(dag_id: str) -> str:
    """Return a deterministic color from the master palette based on dag_id."""
    h = int(hashlib.sha256(dag_id.encode("utf-8")).hexdigest(), 16)
    return DEFAULT_PALETTE[h % len(DEFAULT_PALETTE)]


def get_airflow_server_url() -> str:
    """Return the base Airflow Server URL dynamically."""
    return os.getenv("AIRFLOW_URL", AIRFLOW_URL).rstrip("/")


def get_airflow_api_base() -> str:
    """Return the Airflow REST API base URL."""
    base = get_airflow_server_url()
    if not base.endswith("/api/v1"):
        return f"{base}/api/v1"
    return base


def get_airflow_auth() -> tuple[str, str] | None:
    """Return HTTP basic auth credentials for Airflow."""
    user = os.getenv("AIRFLOW_ADMIN_USER", AIRFLOW_ADMIN_USER)
    pwd = (
        os.getenv("AIRFLOW_ADMIN_PASSWORD")
        or os.getenv("AIRFLOW_PASSWORD")
        or AIRFLOW_ADMIN_PASSWORD
    )
    if pwd:
        return (user, pwd)
    return None


def get_airflow_dags_dir() -> Path:
    """Return the root Airflow DAGs directory."""
    return Path(os.getenv("AIRFLOW_DAGS_DIR", AIRFLOW_DAGS_DIR))


def get_execution_mode() -> str:
    """Return the execution mode: 'k8s' or 'docker'."""
    return os.getenv("EXECUTION_MODE", EXECUTION_MODE).lower()


def sanitize_pipeline_name(name: str) -> str:
    """Sanitize and validate a pipeline title or identifier.

    Blocks directory traversal ('..', '/', '\\') and illegal symbols.
    """
    clean = name.strip()
    if not clean:
        raise ValueError("Pipeline name cannot be empty")
    if ".." in clean or "/" in clean or "\\" in clean:
        raise ValueError(f"Invalid path traversal in pipeline name: {name}")
    # Replace non-alphanumeric (except hyphen/underscore/spaces) with underscore
    clean = re.sub(r"[^a-zA-Z0-9_\-\s]", "_", clean)
    return clean


def sanitize_dag_id(dag_id: str) -> str:
    """Sanitize and validate a DAG ID."""
    clean = dag_id.strip()
    if not clean:
        raise ValueError("DAG ID cannot be empty")
    if ".." in clean or "/" in clean or "\\" in clean:
        raise ValueError(f"Invalid path traversal in DAG ID: {dag_id}")
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", clean)
    return clean


def resolve_workspace_dag_id(workspace_slug: str, pipeline_title: str) -> str:
    """Construct a workspace-scoped DAG ID."""
    clean_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", workspace_slug.strip().lower())
    clean_title = re.sub(r"[^a-zA-Z0-9_-]", "_", pipeline_title.strip().lower()).strip("_")
    if not clean_title:
        clean_title = "job"
    return f"ws_{clean_slug}_{clean_title}"


def is_dag_accessible_by_workspace(
    dag_data: dict[str, Any], workspace_slug: str, is_superadmin: bool = False
) -> bool:
    """Check if a DAG belongs to the specified workspace or if caller is superadmin."""
    if is_superadmin:
        return True
    dag_id = dag_data.get("dag_id", "")
    prefix = f"ws_{workspace_slug}_"
    if dag_id.startswith(prefix) or dag_id == "run_notebook":
        return True
    tags = dag_data.get("tags", [])
    expected_tag = f"workspace:{workspace_slug}"
    for tag in tags:
        tag_name = tag.get("name") if isinstance(tag, dict) else str(tag)
        if tag_name == expected_tag:
            return True
    return False


def _calculate_duration(
    start_str: str | None, end_str: str | None, state: str | None
) -> float | None:
    """Calculate duration in seconds between start and end timestamps."""
    if not start_str:
        return None
    try:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        if end_str:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            return max(0.0, (end_dt - start_dt).total_seconds())
        if state == "running":
            now = datetime.now(UTC)
            return max(0.0, (now - start_dt).total_seconds())
    except Exception:
        pass
    return None


async def _fetch_latest_dag_run(
    client: httpx.AsyncClient, api_base: str, auth: tuple[str, str] | None, dag_id: str
) -> dict[str, Any] | None:
    """Fetch the most recent DAG run for a given pipeline."""
    try:
        url = f"{api_base}/dags/{dag_id}/dagRuns"
        resp = await client.get(
            url,
            params={"order_by": "-execution_date", "limit": 1},
            auth=auth,
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            runs = data.get("dag_runs", [])
            if runs:
                r = runs[0]
                start = r.get("start_date")
                end = r.get("end_date")
                state = r.get("state")
                duration = _calculate_duration(start, end, state)
                return {
                    "dag_run_id": r.get("dag_run_id"),
                    "state": state,
                    "execution_date": r.get("execution_date"),
                    "start_date": start,
                    "end_date": end,
                    "duration_seconds": duration,
                }
    except Exception as exc:
        logger.debug("Failed to fetch latest run for %s: %s", dag_id, exc)
    return None


def _extract_pending_dag_metadata_from_file(
    py_file: Path, workspace_slug: str
) -> dict[str, Any] | None:
    """Parse dynamic DAG python code on disk to synthesize DAG metadata if not yet synced to Airflow."""
    try:
        content = py_file.read_text(encoding="utf-8")
        dag_id_match = re.search(r'dag_id=["\']([^"\']+)["\']', content)
        dag_id = dag_id_match.group(1) if dag_id_match else py_file.stem

        sched_match = re.search(r'schedule(?:_interval)?=["\']([^"\']+)["\']', content)
        schedule = sched_match.group(1) if sched_match else "None (Manual)"

        doc_match = re.search(r'doc_md=["\']([^"\']+)["\']', content)
        doc_md = doc_match.group(1) if doc_match else dag_id.replace("_", " ").title()

        tags_match = re.search(r"tags=\[([^\]]+)\]", content)
        tags = []
        if tags_match:
            raw_tags = tags_match.group(1)
            tags = [t.strip().strip("\"'") for t in raw_tags.split(",") if t.strip()]
        else:
            tags = [f"workspace:{workspace_slug}", "notebook", "scheduled"]

        sched_obj = (
            {"__type": "CronExpression", "value": schedule}
            if (schedule.startswith("@") or " " in schedule)
            else schedule
        )

        return {
            "dag_id": dag_id,
            "description": doc_md,
            "schedule_interval": sched_obj,
            "timetable_description": schedule,
            "is_paused": False,
            "is_active": True,
            "tags": [{"name": t} for t in tags],
            "fileloc": str(py_file),
            "next_dagrun": None,
        }
    except Exception as exc:
        logger.warning("Failed to parse pending DAG metadata from %s: %s", py_file, exc)
        return None


async def list_workspace_dags(
    workspace_slug: str,
    is_superadmin: bool = False,
    workspace_id: uuid.UUID | None = None,
    db: Session | None = None,
) -> list[dict[str, Any]]:
    """List pipelines visible to the current workspace, enriched with latest run info."""
    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"limit": 100}, auth=auth)
            if resp.status_code != 200:
                logger.error("Airflow GET /dags error: %s - %s", resp.status_code, resp.text)
                raise HTTPException(502, f"Failed to list DAGs from Airflow: {resp.text}")
            data = resp.json()
            raw_dags = data.get("dags", [])

            # Filter DAGs by workspace
            matching_dags = [
                d
                for d in raw_dags
                if is_dag_accessible_by_workspace(d, workspace_slug, is_superadmin)
            ]

            # Merge any local workspace DAG files that haven't been picked up by Airflow yet
            existing_dag_ids = {d.get("dag_id") for d in matching_dags}
            dags_dir = get_airflow_dags_dir()
            ws_dir = dags_dir / "workspaces" / workspace_slug
            if ws_dir.is_dir():
                for py_file in sorted(ws_dir.glob("*.py")):
                    if py_file.stem not in existing_dag_ids:
                        pending_dag = _extract_pending_dag_metadata_from_file(
                            py_file, workspace_slug
                        )
                        if pending_dag and is_dag_accessible_by_workspace(
                            pending_dag, workspace_slug, is_superadmin
                        ):
                            matching_dags.append(pending_dag)
                            existing_dag_ids.add(py_file.stem)

            meta_map = get_pipeline_metadata_map(workspace_id, db) if workspace_id and db else {}

            results = []
            for dag in matching_dags:
                dag_id = dag.get("dag_id", "")
                tags_raw = dag.get("tags", [])
                tags = [t.get("name", t) if isinstance(t, dict) else str(t) for t in tags_raw]
                sched_obj = dag.get("schedule_interval")
                cron_val = None
                if isinstance(sched_obj, dict):
                    cron_val = sched_obj.get("value")
                elif sched_obj and str(sched_obj).lower() not in ("none", "null"):
                    cron_val = str(sched_obj)

                # Prioritize cron expression for machine scheduling & projection; fallback to timetable_description
                schedule = cron_val or dag.get("timetable_description") or "None (Manual)"

                last_run = await _fetch_latest_dag_run(client, api_base, auth, dag_id)

                is_nb = (
                    "notebook" in tags
                    or "notebooks" in tags
                    or dag_id.startswith(f"ws_{workspace_slug}_")
                    or dag_id == "run_notebook"
                )

                nb_path = None
                for t in tags:
                    if t.startswith("notebook:"):
                        nb_path = t[len("notebook:") :]
                        break
                if not nb_path and dag_id in KNOWN_PIPELINE_NOTEBOOKS:
                    nb_path = KNOWN_PIPELINE_NOTEBOOKS[dag_id]

                meta = meta_map.get(dag_id)
                color_hex = meta.color_hex if meta else get_default_color_for_dag(dag_id)
                display_label = (
                    meta.display_label
                    if meta and meta.display_label
                    else (dag.get("description") or dag_id.replace("_", " ").title())
                )
                description = (
                    meta.description
                    if meta and meta.description is not None
                    else (dag.get("description") or "")
                )

                results.append(
                    {
                        "dag_id": dag_id,
                        "pipeline_name": display_label,
                        "display_label": display_label,
                        "description": description,
                        "color_hex": color_hex,
                        "is_visible_on_timeline": meta.is_visible_on_timeline if meta else True,
                        "is_paused": dag.get("is_paused", False),
                        "schedule_interval": schedule,
                        "next_dagrun": dag.get("next_dagrun"),
                        "tags": tags,
                        "is_notebook_job": is_nb,
                        "notebook_path": nb_path,
                        "last_run": last_run,
                    }
                )
            return results
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def get_dag_details(
    workspace_slug: str,
    dag_id: str,
    is_superadmin: bool = False,
    workspace_id: uuid.UUID | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Retrieve detailed metadata for a single pipeline."""
    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, auth=auth)
            dag = None
            if resp.status_code == 200:
                dag = resp.json()
            elif resp.status_code == 404:
                # Check if the DAG file exists on disk in workspace folder
                dags_dir = get_airflow_dags_dir()
                dag_file = (
                    dags_dir / "workspaces" / workspace_slug / f"{sanitize_dag_id(dag_id)}.py"
                )
                if dag_file.is_file():
                    dag = _extract_pending_dag_metadata_from_file(dag_file, workspace_slug)

            if not dag:
                if resp.status_code != 200 and resp.status_code != 404:
                    raise HTTPException(502, f"Failed to get DAG from Airflow: {resp.text}")
                raise HTTPException(404, f"Pipeline {dag_id} not found")

            if not is_dag_accessible_by_workspace(dag, workspace_slug, is_superadmin):
                raise HTTPException(404, f"Pipeline {dag_id} not found")

            last_run = (
                await _fetch_latest_dag_run(client, api_base, auth, dag_id)
                if resp.status_code == 200
                else None
            )
            tags_raw = dag.get("tags", [])
            tags = [t.get("name", t) if isinstance(t, dict) else str(t) for t in tags_raw]
            sched_obj = dag.get("schedule_interval")
            cron_val = None
            if isinstance(sched_obj, dict):
                cron_val = sched_obj.get("value")
            elif sched_obj and str(sched_obj).lower() not in ("none", "null"):
                cron_val = str(sched_obj)

            schedule = cron_val or dag.get("timetable_description") or "None (Manual)"

            is_nb = (
                "notebook" in tags
                or "notebooks" in tags
                or dag_id.startswith(f"ws_{workspace_slug}_")
                or dag_id == "run_notebook"
            )

            nb_path = None
            for t in tags:
                if t.startswith("notebook:"):
                    nb_path = t[len("notebook:") :]
                    break
            if not nb_path and dag_id in KNOWN_PIPELINE_NOTEBOOKS:
                nb_path = KNOWN_PIPELINE_NOTEBOOKS[dag_id]

            meta_map = get_pipeline_metadata_map(workspace_id, db) if workspace_id and db else {}
            meta = meta_map.get(dag_id)
            color_hex = meta.color_hex if meta else get_default_color_for_dag(dag_id)
            display_label = (
                meta.display_label
                if meta and meta.display_label
                else (dag.get("description") or dag_id.replace("_", " ").title())
            )
            description = (
                meta.description
                if meta and meta.description is not None
                else (dag.get("description") or "")
            )

            return {
                "dag_id": dag_id,
                "pipeline_name": display_label,
                "display_label": display_label,
                "description": description,
                "color_hex": color_hex,
                "is_visible_on_timeline": meta.is_visible_on_timeline if meta else True,
                "is_paused": dag.get("is_paused", False),
                "schedule_interval": schedule,
                "next_dagrun": dag.get("next_dagrun"),
                "tags": tags,
                "is_notebook_job": is_nb,
                "notebook_path": nb_path,
                "last_run": last_run,
                "fileloc": dag.get("fileloc"),
                "max_active_runs": dag.get("max_active_runs"),
            }

    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def set_dag_paused(
    workspace_slug: str, dag_id: str, is_paused: bool, is_superadmin: bool = False
) -> dict[str, Any]:
    """Pause or unpause a workspace pipeline."""
    # Verify access first
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, json={"is_paused": is_paused}, auth=auth)
            if resp.status_code != 200:
                raise HTTPException(
                    resp.status_code, f"Failed to update DAG pause state: {resp.text}"
                )
            return {
                "dag_id": dag_id,
                "is_paused": is_paused,
                "message": f"Pipeline {'paused' if is_paused else 'unpaused'} successfully",
            }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def trigger_dag_run(
    workspace_slug: str,
    dag_id: str,
    conf: dict[str, Any] | None = None,
    triggered_by: str = "system",
    is_superadmin: bool = False,
) -> dict[str, Any]:
    """Trigger manual execution of a workspace pipeline."""
    # Verify access first
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    clean_user = re.sub(r"[^a-zA-Z0-9_]", "_", triggered_by.split("@")[0])
    run_id = f"manual__{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}__{clean_user}"
    payload = {
        "dag_run_id": run_id,
        "conf": {
            **(conf or {}),
            "workspace_slug": workspace_slug,
            "triggered_by": triggered_by,
        },
    }

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, auth=auth)
            if resp.status_code not in (200, 201):
                raise HTTPException(resp.status_code, f"Failed to trigger DAG run: {resp.text}")
            data = resp.json()
            return {
                "dag_id": dag_id,
                "dag_run_id": data.get("dag_run_id", run_id),
                "state": data.get("state", "queued"),
                "execution_date": data.get("execution_date"),
                "conf": data.get("conf", payload["conf"]),
            }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def list_dag_runs(
    workspace_slug: str, dag_id: str, limit: int = 25, is_superadmin: bool = False
) -> list[dict[str, Any]]:
    """List historical execution runs for a workspace pipeline."""
    # Verify access first
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                params={"order_by": "-execution_date", "limit": limit},
                auth=auth,
            )
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, f"Failed to fetch DAG runs: {resp.text}")
            data = resp.json()
            runs = data.get("dag_runs", [])
            results = []
            for r in runs:
                start = r.get("start_date")
                end = r.get("end_date")
                state = r.get("state")
                results.append(
                    {
                        "dag_run_id": r.get("dag_run_id"),
                        "state": state,
                        "execution_date": r.get("execution_date"),
                        "start_date": start,
                        "end_date": end,
                        "duration_seconds": _calculate_duration(start, end, state),
                        "conf": r.get("conf", {}),
                    }
                )
            return results
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def get_dag_run(
    workspace_slug: str, dag_id: str, run_id: str, is_superadmin: bool = False
) -> dict[str, Any]:
    """Get single DAG run details."""
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns/{run_id}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, auth=auth)
            if resp.status_code == 404:
                raise HTTPException(404, f"Run {run_id} not found")
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, f"Failed to fetch DAG run: {resp.text}")
            r = resp.json()
            start = r.get("start_date")
            end = r.get("end_date")
            state = r.get("state")
            return {
                "dag_run_id": r.get("dag_run_id"),
                "state": state,
                "execution_date": r.get("execution_date"),
                "start_date": start,
                "end_date": end,
                "duration_seconds": _calculate_duration(start, end, state),
                "conf": r.get("conf", {}),
            }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def list_task_instances(
    workspace_slug: str, dag_id: str, run_id: str, is_superadmin: bool = False
) -> list[dict[str, Any]]:
    """List task instances for a specific pipeline execution run."""
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns/{run_id}/taskInstances"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, auth=auth)
            if resp.status_code != 200:
                raise HTTPException(
                    resp.status_code, f"Failed to fetch task instances: {resp.text}"
                )
            data = resp.json()
            tasks = data.get("task_instances", [])
            results = []
            for t in tasks:
                start = t.get("start_date")
                end = t.get("end_date")
                state = t.get("state")
                results.append(
                    {
                        "task_id": t.get("task_id"),
                        "state": state,
                        "try_number": t.get("try_number", 1),
                        "start_date": start,
                        "end_date": end,
                        "duration_seconds": _calculate_duration(start, end, state),
                    }
                )
            return results
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def get_task_log(
    workspace_slug: str,
    dag_id: str,
    run_id: str,
    task_id: str,
    try_number: int | None = None,
    is_superadmin: bool = False,
) -> dict[str, Any]:
    """Fetch task instance execution logs."""
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    actual_try = try_number or 1
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns/{run_id}/taskInstances/{task_id}/logs/{actual_try}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, auth=auth)
            if resp.status_code == 404:
                return {
                    "dag_id": dag_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "try_number": actual_try,
                    "content": "No logs recorded yet.",
                }
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, f"Failed to fetch task logs: {resp.text}")

            try:
                data = resp.json()
                content = data.get("content", resp.text)
            except Exception:
                content = resp.text

            return {
                "dag_id": dag_id,
                "run_id": run_id,
                "task_id": task_id,
                "try_number": actual_try,
                "content": content,
            }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


def generate_dynamic_dag_code(
    workspace_slug: str,
    dag_id: str,
    notebook_path: str,
    schedule_interval: str,
    pipeline_title: str,
    parameters: dict[str, Any] | None = None,
    compute_resources: dict[str, str] | None = None,
    mode: str | None = None,
) -> str:
    """Generate syntactically valid Python Airflow DAG code for recurring notebook scheduling."""
    exec_mode = (mode or get_execution_mode()).lower()
    clean_slug = workspace_slug.strip()
    clean_params = json.dumps(parameters or {})
    clean_title = pipeline_title.replace('"', '\\"')
    clean_nb = notebook_path.strip("/ ")
    clean_sched = schedule_interval.strip()

    cpu_req = (compute_resources or {}).get("cpu", "500m")
    mem_req = (compute_resources or {}).get("memory", "1Gi")
    cpu_limit = "2" if cpu_req in ("500m", "1") else cpu_req
    mem_limit = "4Gi" if mem_req in ("1Gi", "2Gi") else mem_req

    if exec_mode == "k8s":
        code = f"""# Dynamic Lake of Tears Pipeline DAG (Kubernetes Compute Isolation)
# Generated automatically for workspace: {clean_slug}

from __future__ import annotations

import json
from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from kubernetes.client import models as k8s

default_args = {{
    "owner": "lake-of-tears",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(hours=2),
}}

with DAG(
    dag_id="{dag_id}",
    schedule="{clean_sched}",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:{clean_slug}", "notebook", "scheduled", "notebook:{clean_nb}"],
    doc_md="{clean_title}",
    default_args=default_args,
) as dag:
    run_notebook_task = KubernetesPodOperator(
        task_id="execute_and_archive_notebook",
        name="lake-nb-{clean_slug}",
        namespace="lake-of-tears",
        image="jupyter/datascience-notebook:latest",
        cmds=["papermill"],
        arguments=[
            "/workspace/{clean_nb}",
            "/workspace/output.ipynb",
            "-f",
            "/workspace/params.json",
        ],
        container_resources=k8s.V1ResourceRequirements(
            requests={{"cpu": "{cpu_req}", "memory": "{mem_req}"}},
            limits={{"cpu": "{cpu_limit}", "memory": "{mem_limit}"}},
        ),
        is_delete_operator_pod=True,
        get_logs=True,
    )
"""
    else:
        code = f"""# Dynamic Lake of Tears Pipeline DAG (Docker / Worker Compute Isolation)
# Generated automatically for workspace: {clean_slug}

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator

logger = logging.getLogger("airflow.task.{dag_id}")

default_args = {{
    "owner": "lake-of-tears",
    "retries": 1,
    "retry_delay": timedelta(seconds=10),
    "execution_timeout": timedelta(hours=2),
}}

def _run_notebook_callable(**context):
    import boto3
    import papermill as pm
    import requests

    workspace_slug = "{clean_slug}"
    notebook_path = "{clean_nb}"
    raw_params = {clean_params}
    execution_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")

    logger.info("Executing scheduled notebook %s for workspace %s", notebook_path, workspace_slug)

    # 1. Fetch source notebook from Jupyter server
    jupyter_url = os.getenv("JUPYTER_SERVER_URL", "http://jupyter:8888").rstrip("/")
    token = os.getenv("JUPYTER_TOKEN", "")
    headers = {{"Authorization": f"token {{token}}"}} if token else {{}}
    nb_url = f"{{jupyter_url}}/jupyter/api/contents/workspaces/{{workspace_slug}}/{{notebook_path}}"
    resp = requests.get(nb_url, headers=headers, params={{"type": "notebook"}}, timeout=30)
    resp.raise_for_status()
    nb_dict = resp.json().get("content")

    # 2. Execute with Papermill in isolated directory
    with tempfile.TemporaryDirectory() as tmpdir:
        in_file = os.path.join(tmpdir, "input.ipynb")
        out_file = os.path.join(tmpdir, "output.ipynb")
        with open(in_file, "w", encoding="utf-8") as f:
            json.dump(nb_dict, f)
        pm.execute_notebook(input_path=in_file, output_path=out_file, parameters=raw_params, log_output=True)

        # 3. Archive to MinIO S3
        minio_endpoint = os.getenv("MINIO_ENDPOINT", "minio:9000")
        minio_access = os.getenv("MINIO_ACCESS_KEY", "")
        minio_secret = os.getenv("MINIO_SECRET_KEY", "")
        bucket = os.getenv("MINIO_BUCKET", "datalake")
        s3_key = f"notebook_runs/{{workspace_slug}}/{{notebook_path.replace('.ipynb', '')}}/{{execution_ts}}.ipynb"

        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://{{minio_endpoint}}",
            aws_access_key_id=minio_access,
            aws_secret_access_key=minio_secret,
            region_name="us-east-1",
        )
        with open(out_file, "rb") as f:
            s3.upload_fileobj(f, bucket, s3_key)

    logger.info("Successfully archived notebook run to s3://%s/%s", bucket, s3_key)
    return {{"status": "success", "s3_key": s3_key}}

with DAG(
    dag_id="{dag_id}",
    schedule="{clean_sched}",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["workspace:{clean_slug}", "notebook", "scheduled", "notebook:{clean_nb}"],
    doc_md="{clean_title}",
    default_args=default_args,
) as dag:
    run_task = PythonOperator(
        task_id="execute_and_archive_notebook",
        python_callable=_run_notebook_callable,
    )
"""

    # Validate generated Python syntax
    ast.parse(code)
    return code


async def schedule_notebook(
    workspace_slug: str,
    notebook_path: str,
    schedule_interval: str,
    pipeline_title: str | None = None,
    parameters: dict[str, Any] | None = None,
    compute_resources: dict[str, str] | None = None,
    is_superadmin: bool = False,
    workspace_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Create or update a recurring notebook schedule DAG."""
    clean_title = sanitize_pipeline_name(
        pipeline_title or Path(notebook_path).stem.replace("_", " ").title()
    )
    dag_id = resolve_workspace_dag_id(workspace_slug, clean_title)

    code = generate_dynamic_dag_code(
        workspace_slug=workspace_slug,
        dag_id=dag_id,
        notebook_path=notebook_path,
        schedule_interval=schedule_interval,
        pipeline_title=clean_title,
        parameters=parameters,
        compute_resources=compute_resources,
    )

    dags_dir = get_airflow_dags_dir()
    ws_dir = dags_dir / "workspaces" / workspace_slug
    try:
        ws_dir.mkdir(parents=True, exist_ok=True)
        dag_file = ws_dir / f"{dag_id}.py"
        dag_file.write_text(code, encoding="utf-8")
        logger.info("Wrote dynamic DAG file: %s", dag_file)
    except Exception as exc:
        logger.error("Failed to write dynamic DAG file to %s: %s", ws_dir, exc)
        raise HTTPException(500, f"Failed to persist pipeline DAG file: {exc}") from exc

    # Persist or update PipelineMetadata in DB if workspace_id and db are provided
    if workspace_id and db:
        try:
            upsert_pipeline_metadata(
                workspace_id=workspace_id,
                dag_id=dag_id,
                display_label=clean_title,
                description=f"Scheduled notebook: {notebook_path.strip('/ ')}",
                user_id=user_id,
                db=db,
            )
        except Exception as exc:
            logger.warning("Failed to upsert pipeline metadata for %s: %s", dag_id, exc)

    # Attempt to unpause immediately in Airflow if already registered
    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.patch(f"{api_base}/dags/{dag_id}", json={"is_paused": False}, auth=auth)
    except Exception:
        pass

    return {
        "dag_id": dag_id,
        "file_path": str(dag_file),
        "schedule_interval": schedule_interval,
        "status": "created",
    }


async def delete_scheduled_notebook(
    workspace_slug: str,
    dag_id: str,
    is_superadmin: bool = False,
    workspace_id: uuid.UUID | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Delete a dynamically generated notebook schedule DAG."""
    clean_id = sanitize_dag_id(dag_id)
    if (
        clean_id in PROTECTED_SYSTEM_DAGS
        or clean_id.startswith("ingest_")
        or not clean_id.startswith("ws_")
    ):
        raise HTTPException(400, f"Cannot delete protected or non-workspace pipeline: {dag_id}")

    if not clean_id.startswith(f"ws_{workspace_slug}_"):
        raise HTTPException(404, f"Pipeline {dag_id} not found in this workspace")

    dags_dir = get_airflow_dags_dir()
    dag_file = dags_dir / "workspaces" / workspace_slug / f"{clean_id}.py"
    if dag_file.exists():
        try:
            dag_file.unlink()
            logger.info("Deleted dynamic DAG file: %s", dag_file)
        except Exception as exc:
            logger.warning("Failed to unlink DAG file %s: %s", dag_file, exc)

    # Remove PipelineMetadata from DB if available
    if workspace_id and db:
        try:
            from models import PipelineMetadata

            row = (
                db.query(PipelineMetadata)
                .filter(
                    PipelineMetadata.workspace_id == workspace_id,
                    PipelineMetadata.dag_id == clean_id,
                )
                .first()
            )
            if row:
                db.delete(row)
                db.commit()
        except Exception as exc:
            logger.warning("Failed to delete pipeline metadata for %s: %s", clean_id, exc)

    # Attempt to purge from Airflow REST API
    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{clean_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(url, auth=auth)
            logger.info("Airflow DELETE /dags/%s response: %s", clean_id, resp.status_code)
    except Exception as exc:
        logger.warning("Could not delete DAG from Airflow API: %s", exc)

    return {"dag_id": clean_id, "status": "deleted"}


def compute_projected_runs_for_day(
    schedule_interval: str | None,
    day_start: datetime,
    day_end: datetime,
    max_runs: int = 48,
) -> list[dict[str, Any]]:
    """Compute projected future or scheduled execution points for a cron expression within [day_start, day_end]."""
    if not schedule_interval or schedule_interval.lower() in ("none", "null", "@once", ""):
        return []

    raw = schedule_interval.strip()
    cron_expr = PRESET_MAP.get(raw, raw)

    # Translate common human timetable descriptions if not already valid cron
    if not croniter.is_valid(cron_expr):
        m_at = re.match(r"^At\s+(\d{1,2}):(\d{2})$", raw, re.IGNORECASE)
        if m_at:
            cron_expr = f"{int(m_at.group(2))} {int(m_at.group(1))} * * *"
        elif re.match(r"^Every\s+hour$", raw, re.IGNORECASE):
            cron_expr = "0 * * * *"
        elif re.match(r"^Every\s+(\d+)\s+hours?$", raw, re.IGNORECASE):
            m_h = re.match(r"^Every\s+(\d+)\s+hours?$", raw, re.IGNORECASE)
            cron_expr = f"0 */{m_h.group(1)} * * *"

    if not croniter.is_valid(cron_expr):
        return []

    projected: list[dict[str, Any]] = []
    try:
        user_tz = day_start.tzinfo or UTC
        # Airflow schedules execute in UTC by default
        day_start_utc = day_start.astimezone(UTC)
        day_end_utc = day_end.astimezone(UTC)

        base_time = day_start_utc - timedelta(seconds=1)
        iter_cron = croniter(cron_expr, base_time)
        count = 0
        while count < max_runs:
            next_dt_utc = iter_cron.get_next(datetime)
            if next_dt_utc > day_end_utc:
                break
            # Convert to local timezone for the response
            next_dt_local = next_dt_utc.astimezone(user_tz)
            projected.append(
                {
                    "type": "projected",
                    "dag_run_id": f"projected__{next_dt_utc.strftime('%Y%m%dT%H%M%SZ')}",
                    "state": "scheduled",
                    "scheduled_time": next_dt_local.isoformat(),
                    "start_time": next_dt_local.isoformat(),
                    "end_time": None,
                    "duration_seconds": None,
                }
            )
            count += 1
    except Exception as exc:
        logger.debug("Failed computing cron projected runs: %s", exc)

    return projected


def get_pipeline_metadata_map(workspace_id: uuid.UUID | None, db: Session | None) -> dict[str, Any]:
    """Retrieve color and display label metadata for a workspace's pipelines."""
    if not workspace_id or not db:
        return {}
    try:
        from models import PipelineMetadata

        rows = (
            db.query(PipelineMetadata).filter(PipelineMetadata.workspace_id == workspace_id).all()
        )
        return {row.dag_id: row for row in rows}
    except Exception as exc:
        logger.warning("Failed to query pipeline_metadata: %s", exc)
        return {}


def upsert_pipeline_metadata(
    workspace_id: uuid.UUID,
    dag_id: str,
    color_hex: str | None = None,
    display_label: str | None = None,
    description: str | None = None,
    is_visible_on_timeline: bool | None = None,
    user_id: uuid.UUID | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Persist or update workspace-shared pipeline color, label, and description metadata."""
    if not db:
        raise HTTPException(500, "Database session required")

    from models import PipelineMetadata

    row = (
        db.query(PipelineMetadata)
        .filter(
            PipelineMetadata.workspace_id == workspace_id,
            PipelineMetadata.dag_id == dag_id,
        )
        .first()
    )
    if not row:
        row = PipelineMetadata(
            workspace_id=workspace_id,
            dag_id=dag_id,
            color_hex=color_hex or get_default_color_for_dag(dag_id),
            display_label=display_label or dag_id.replace("_", " ").title(),
            description=description,
            is_visible_on_timeline=True
            if is_visible_on_timeline is None
            else is_visible_on_timeline,
            updated_by=user_id,
            updated_at=datetime.now(UTC),
        )
        db.add(row)
    else:
        if color_hex is not None:
            row.color_hex = color_hex
        if display_label is not None:
            row.display_label = display_label
        if description is not None:
            row.description = description
        if is_visible_on_timeline is not None:
            row.is_visible_on_timeline = is_visible_on_timeline
        row.updated_by = user_id
        row.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(row)
    return {
        "dag_id": row.dag_id,
        "color_hex": row.color_hex,
        "display_label": row.display_label,
        "description": row.description,
        "is_visible_on_timeline": row.is_visible_on_timeline,
    }


async def cancel_dag_run(
    workspace_slug: str,
    dag_id: str,
    run_id: str,
    is_superadmin: bool = False,
) -> dict[str, Any]:
    """Cancel (terminate / mark failed) an active or queued DAG run."""
    await get_dag_details(workspace_slug, dag_id, is_superadmin)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()
    url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns/{run_id}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, json={"state": "failed"}, auth=auth)
            if resp.status_code == 404:
                raise HTTPException(404, f"Run {run_id} not found")
            if resp.status_code not in (200, 201):
                raise HTTPException(resp.status_code, f"Failed to cancel DAG run: {resp.text}")
            r = resp.json()
            return {
                "dag_id": dag_id,
                "dag_run_id": r.get("dag_run_id", run_id),
                "state": r.get("state", "failed"),
                "message": "DAG run cancelled successfully",
            }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Airflow connection error: %s", exc)
        raise HTTPException(503, f"Airflow service unavailable: {exc}") from exc


async def get_workspace_timeline(
    workspace_slug: str,
    workspace_id: uuid.UUID | None,
    target_date_str: str | None = None,
    tz_str: str = "UTC",
    is_superadmin: bool = False,
    db: Session | None = None,
) -> dict[str, Any]:
    """Calculate 24-hour visual schedule timeline merging actual runs and projected future runs."""
    try:
        user_tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        user_tz = zoneinfo.ZoneInfo("UTC")

    now_tz = datetime.now(user_tz)
    if target_date_str:
        try:
            parsed_d = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except Exception:
            parsed_d = now_tz.date()
    else:
        parsed_d = now_tz.date()

    day_start = datetime(parsed_d.year, parsed_d.month, parsed_d.day, 0, 0, 0, tzinfo=user_tz)
    day_end = datetime(
        parsed_d.year, parsed_d.month, parsed_d.day, 23, 59, 59, 999999, tzinfo=user_tz
    )
    day_start_utc = day_start.astimezone(UTC)
    day_end_utc = day_end.astimezone(UTC)

    is_today = parsed_d == now_tz.date()

    # 1. Fetch pipelines and metadata
    pipelines = await list_workspace_dags(workspace_slug, is_superadmin)
    meta_map = get_pipeline_metadata_map(workspace_id, db)

    api_base = get_airflow_api_base()
    auth = get_airflow_auth()

    timeline_pipelines: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for p in pipelines:
            dag_id = p["dag_id"]
            meta = meta_map.get(dag_id)
            color_hex = (
                meta.color_hex if meta and meta.color_hex else get_default_color_for_dag(dag_id)
            )
            display_label = (
                meta.display_label
                if meta and meta.display_label
                else p.get("description") or dag_id.replace("_", " ").title()
            )
            is_visible = meta.is_visible_on_timeline if meta else True

            actual_runs: list[dict[str, Any]] = []
            try:
                url = f"{api_base}/dags/{sanitize_dag_id(dag_id)}/dagRuns"
                resp = await client.get(
                    url,
                    params={
                        "order_by": "-execution_date",
                        "limit": 50,
                    },
                    auth=auth,
                )
                if resp.status_code == 200:
                    runs_data = resp.json().get("dag_runs", [])
                    for r in runs_data:
                        start = r.get("start_date")
                        end = r.get("end_date")
                        state = r.get("state")
                        duration = _calculate_duration(start, end, state)

                        # Determine if this run belongs to the viewed day
                        # A run belongs to this day if its start_date, data_interval_end,
                        # or execution_date falls within [day_start_utc, day_end_utc]
                        candidates: list[datetime] = []
                        for k in ("start_date", "data_interval_end", "execution_date"):
                            v = r.get(k)
                            if v:
                                try:
                                    candidates.append(
                                        datetime.fromisoformat(v.replace("Z", "+00:00"))
                                    )
                                except Exception:
                                    pass

                        matched = [dt for dt in candidates if day_start_utc <= dt <= day_end_utc]
                        if not matched:
                            continue

                        # Reference time in local timezone
                        ref_dt = matched[0].astimezone(user_tz)
                        start_local = (
                            datetime.fromisoformat(start.replace("Z", "+00:00"))
                            .astimezone(user_tz)
                            .isoformat()
                            if start
                            else ref_dt.isoformat()
                        )
                        end_local = (
                            datetime.fromisoformat(end.replace("Z", "+00:00"))
                            .astimezone(user_tz)
                            .isoformat()
                            if end
                            else None
                        )
                        actual_runs.append(
                            {
                                "type": "actual",
                                "dag_run_id": r.get("dag_run_id"),
                                "state": state,
                                "scheduled_time": ref_dt.isoformat(),
                                "start_time": start_local,
                                "end_time": end_local,
                                "duration_seconds": duration,
                                "conf": r.get("conf", {}),
                            }
                        )
            except Exception as exc:
                logger.debug("Failed fetching runs for timeline DAG %s: %s", dag_id, exc)

            # 2. Compute projected runs if pipeline is active and has schedule
            projected_runs: list[dict[str, Any]] = []
            if not p.get("is_paused") and p.get("schedule_interval"):
                raw_projected = compute_projected_runs_for_day(
                    p["schedule_interval"], day_start, day_end
                )
                for proj in raw_projected:
                    proj_dt_utc = datetime.fromisoformat(proj["scheduled_time"]).astimezone(UTC)
                    # Check if actual run exists within 60s of this scheduled slot
                    already_ran = any(
                        abs(
                            (
                                datetime.fromisoformat(act["scheduled_time"]).astimezone(UTC)
                                - proj_dt_utc
                            ).total_seconds()
                        )
                        < 60
                        for act in actual_runs
                    )
                    if not already_ran:
                        projected_runs.append(proj)

            # 3. Include next_dagrun from Airflow if available and falls within this day
            if not p.get("is_paused") and p.get("next_dagrun"):
                try:
                    next_dt_utc = datetime.fromisoformat(p["next_dagrun"].replace("Z", "+00:00"))
                    if day_start_utc <= next_dt_utc <= day_end_utc:
                        next_dt_local = next_dt_utc.astimezone(user_tz)
                        already_included = any(
                            abs(
                                (
                                    datetime.fromisoformat(r["scheduled_time"]).astimezone(UTC)
                                    - next_dt_utc
                                ).total_seconds()
                            )
                            < 60
                            for r in actual_runs + projected_runs
                        )
                        if not already_included:
                            projected_runs.append(
                                {
                                    "type": "projected",
                                    "dag_run_id": f"projected__{next_dt_utc.strftime('%Y%m%dT%H%M%SZ')}",
                                    "state": "scheduled",
                                    "scheduled_time": next_dt_local.isoformat(),
                                    "start_time": next_dt_local.isoformat(),
                                    "end_time": None,
                                    "duration_seconds": None,
                                }
                            )
                except Exception as exc:
                    logger.debug("Failed parsing next_dagrun: %s", exc)

            combined_runs = sorted(
                actual_runs + projected_runs,
                key=lambda x: x.get("start_time") or x.get("scheduled_time") or "",
            )

            timeline_pipelines.append(
                {
                    "dag_id": dag_id,
                    "display_label": display_label,
                    "color_hex": color_hex,
                    "is_visible_on_timeline": is_visible,
                    "schedule_interval": p.get("schedule_interval"),
                    "is_paused": p.get("is_paused", False),
                    "runs": combined_runs,
                }
            )

    return {
        "date": parsed_d.strftime("%Y-%m-%d"),
        "timezone": str(user_tz),
        "window_start": day_start.isoformat(),
        "window_end": day_end.isoformat(),
        "current_time": now_tz.isoformat(),
        "is_today": is_today,
        "palette": DEFAULT_PALETTE,
        "pipelines": timeline_pipelines,
    }
