"""Playwright check for the pipeline inspector drawer Refresh button (#479).

Renders the real /pipelines template, serves it from a fake origin, and mocks the
pipeline API so clicking the header Refresh can be verified to re-fetch both the
pipeline details and the run list and to update Last Run / Next Run in place.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from starlette.testclient import TestClient

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    sync_playwright = None

if not HAS_PLAYWRIGHT:
    pytestmark = pytest.mark.skip(reason="playwright is not installed")

sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

ORIGIN = "http://lake.test"
DAG_ID = "ws_analytics_email"

STALE_PIPELINE = {
    "dag_id": DAG_ID,
    "pipeline_name": "Email",
    "display_label": "Email",
    "description": "Email ingestion",
    "is_paused": False,
    "schedule_interval": "0 1,7,13,19 * * *",
    "next_dagrun": "2026-11-11T19:00:00+00:00",
    "next_run_at": "2026-11-12T01:00:00+00:00",
    "tags": ["workspace:analytics"],
    "last_run": {
        "dag_run_id": "manual__20261111T235625Z__member",
        "state": "failed",
        "start_date": "2026-11-11T23:56:26+00:00",
        "duration_seconds": 94.4,
    },
}

FRESH_PIPELINE = {
    **STALE_PIPELINE,
    "next_dagrun": "2026-11-12T01:00:00+00:00",
    "next_run_at": "2026-11-12T07:00:00+00:00",
    "last_run": {
        "dag_run_id": "scheduled__2026-11-11T19:00:00+00:00",
        "state": "success",
        "start_date": "2026-11-12T01:00:00.532654+00:00",
        "duration_seconds": 91.9,
    },
}

FRESH_RUNS = [
    {
        "dag_run_id": "scheduled__2026-11-11T19:00:00+00:00",
        "state": "success",
        "run_type": "scheduled",
        "execution_date": "2026-11-11T19:00:00+00:00",
        "data_interval_start": "2026-11-11T19:00:00+00:00",
        "data_interval_end": "2026-11-12T01:00:00+00:00",
        "start_date": "2026-11-12T01:00:00.532654+00:00",
        "end_date": "2026-11-12T01:01:32.449156+00:00",
        "duration_seconds": 91.9,
    },
    {
        "dag_run_id": "manual__20261111T235625Z__member",
        "state": "failed",
        "run_type": "manual",
        "execution_date": "2026-11-11T23:56:25+00:00",
        "start_date": "2026-11-11T23:56:26+00:00",
        "end_date": "2026-11-11T23:58:00+00:00",
        "duration_seconds": 94.4,
    },
]


def _render_pipelines_page() -> str:
    token = jwt.encode(
        {"sub": "member@lake.local", "email": "member@lake.local", "role": "user"},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": "member"}]
    with patch.object(main, "_fetch_workspaces", return_value=workspaces):
        res = TestClient(main.app).get(
            "/pipelines", cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}
        )
    assert res.status_code == 200
    return res.text


def test_drawer_refresh_reloads_details_and_runs():
    html = _render_pipelines_page()
    calls: list[str] = []
    state = {"fresh": False}

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN) :].split("?", 1)[0]
        if not url.startswith(ORIGIN):
            return route.abort()
        if path == "/pipelines":
            return route.fulfill(status=200, content_type="text/html", body=html)
        if not path.startswith("/api/"):
            return route.fulfill(status=200, content_type="application/javascript", body="")
        calls.append(path)
        if path == "/api/pipelines":
            body = {"pipelines": [FRESH_PIPELINE if state["fresh"] else STALE_PIPELINE]}
        elif path == f"/api/pipelines/{DAG_ID}":
            body = FRESH_PIPELINE if state["fresh"] else STALE_PIPELINE
        elif path == f"/api/pipelines/{DAG_ID}/runs":
            body = {"runs": FRESH_RUNS if state["fresh"] else FRESH_RUNS[1:]}
        else:
            body = {}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(timezone_id="America/New_York", locale="en-US")
            page.route("**/*", handle)
            page.goto(f"{ORIGIN}/pipelines")
            page.wait_for_function("typeof window.refreshInspector === 'function'")
            page.evaluate("fetchPipelines()")
            page.wait_for_function(
                f"(window.allPipelines || allPipelines).some(p => p.dag_id === '{DAG_ID}')"
            )
            page.evaluate(f"openInspector('{DAG_ID}')")
            page.wait_for_selector("#drawer-runs-list [id^='drawer-run-']")

            assert page.inner_text("#drawer-meta-next-run") == "11/11/2026, 8:00:00 PM"
            assert "Failed" in page.inner_text("#drawer-meta-last-run-badge")
            assert page.inner_text("#drawer-runs-count") == "1"

            state["fresh"] = True
            calls.clear()
            page.click("#btn-drawer-refresh")
            page.wait_for_function(
                "document.getElementById('drawer-last-refreshed').textContent.startsWith('Updated')"
            )

            assert f"/api/pipelines/{DAG_ID}" in calls
            assert f"/api/pipelines/{DAG_ID}/runs" in calls
            assert page.inner_text("#drawer-meta-next-run") == "11/12/2026, 2:00:00 AM"
            assert "Success" in page.inner_text("#drawer-meta-last-run-badge")
            assert page.inner_text("#drawer-meta-duration") == "92s"
            assert page.inner_text("#drawer-runs-count") == "2"

            first_card = page.inner_text("#drawer-runs-list > div:first-child")
            assert "scheduled__2026-11-11T19:00:00+00:00" in first_card
            assert "Started: 11/11/2026, 8:00:00 PM" in first_card
            assert "Covers 11/11/2026, 2:00:00 PM → 11/11/2026, 8:00:00 PM" in first_card
            assert not page.is_disabled("#btn-drawer-refresh")
        finally:
            browser.close()
