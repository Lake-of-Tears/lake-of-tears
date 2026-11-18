"""Tests for the /storage stats cache and async page render (#568)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import storage_service  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    storage_service.invalidate_bucket_summary()
    storage_service._summary_refreshing.clear()
    yield
    storage_service.invalidate_bucket_summary()
    storage_service._summary_refreshing.clear()


def _summary(n: int = 1) -> dict:
    return {"ok": True, "total_objects": n, "total_size": "1 B"}


def test_second_call_within_ttl_does_not_rescan():
    with patch.object(storage_service, "get_bucket_summary", return_value=_summary()) as scan:
        first = storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        second = storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
    assert scan.call_count == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["as_of"] == first["as_of"]


def test_refresh_bypasses_cache():
    with patch.object(storage_service, "get_bucket_summary", return_value=_summary()) as scan:
        storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        res = storage_service.get_bucket_summary_cached(
            workspace_slug="a", workspace_id="1", refresh=True
        )
    assert scan.call_count == 2
    assert res["cached"] is False


def test_workspaces_are_isolated():
    def fake(workspace_slug=None, **_):
        return _summary(10 if workspace_slug == "a" else 20)

    with patch.object(storage_service, "get_bucket_summary", side_effect=fake) as scan:
        a = storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        b = storage_service.get_bucket_summary_cached(workspace_slug="b", workspace_id="2")
    assert (a["total_objects"], b["total_objects"]) == (10, 20)
    assert scan.call_count == 2


def test_invalidate_only_clears_target_workspace():
    with patch.object(storage_service, "get_bucket_summary", return_value=_summary()) as scan:
        storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        storage_service.get_bucket_summary_cached(workspace_slug="b", workspace_id="2")
        storage_service.invalidate_bucket_summary(workspace_slug="a", workspace_id="1")
        storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        storage_service.get_bucket_summary_cached(workspace_slug="b", workspace_id="2")
    assert scan.call_count == 3


def test_failed_scan_is_not_cached():
    with patch.object(
        storage_service, "get_bucket_summary", return_value={"ok": False, "error": "boom"}
    ) as scan:
        storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
    assert scan.call_count == 2


def test_stale_entry_served_instantly_with_single_background_refresh():
    key = storage_service._summary_cache_key("a", "1")
    old = time.time() - storage_service.STORAGE_STATS_TTL_SECONDS - 10
    storage_service._summary_cache[key] = (old, _summary(1))

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_scan(**kwargs):
        calls.append(kwargs)
        started.set()
        release.wait(5)
        return _summary(2)

    with (
        patch.object(storage_service, "get_bucket_summary", side_effect=slow_scan),
        patch("database.SessionLocal"),
    ):
        r1 = storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        assert started.wait(2)
        r2 = storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
        # Both calls returned the stale value without waiting on the scan.
        assert r1["total_objects"] == 1 and r1["cached"] is True
        assert r2["total_objects"] == 1
        release.set()
        deadline = time.time() + 2
        while key in storage_service._summary_refreshing and time.time() < deadline:
            time.sleep(0.01)

    assert len(calls) == 1
    assert storage_service._summary_cache[key][1]["total_objects"] == 2


def test_entry_past_max_age_recomputes_synchronously():
    key = storage_service._summary_cache_key("a", "1")
    ancient = time.time() - storage_service.STORAGE_STATS_MAX_AGE_SECONDS - 10
    storage_service._summary_cache[key] = (ancient, _summary(1))
    with patch.object(storage_service, "get_bucket_summary", return_value=_summary(9)) as scan:
        res = storage_service.get_bucket_summary_cached(workspace_slug="a", workspace_id="1")
    assert scan.call_count == 1
    assert res["total_objects"] == 9 and res["cached"] is False


# ── UI: page render must not scan the bucket ──────────────────────────────────


def test_storage_page_render_does_not_scan_bucket():
    import main

    ui_client = TestClient(main.app)
    token = jwt.encode(
        {"sub": "m@lake.local", "email": "m@lake.local", "role": "user"},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": "member"}]
    with (
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(main, "_get_bucket_stats") as scan,
        patch.object(main, "_s3_client") as s3,
    ):
        resp = ui_client.get(
            "/storage", cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}
        )
    assert resp.status_code == 200
    scan.assert_not_called()
    s3.assert_not_called()
    assert 'id="stat-total-objects"' in resp.text
    assert "Calculating" in resp.text
    assert "/api/v1/storage/stats" in resp.text
    assert 'id="btn-refresh-stats"' in resp.text
