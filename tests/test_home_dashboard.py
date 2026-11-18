"""Tests for the root Dashboard: async stats, real pipeline health, no fake connectors (#571)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import storage_service  # noqa: E402


def _client_and_cookies():
    import main

    token = jwt.encode(
        {"sub": "m@lake.local", "email": "m@lake.local", "role": "user"},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    return main, TestClient(main.app), {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}


WORKSPACES = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": "member"}]


def test_home_render_does_not_scan_bucket_or_call_stats():
    main, client, cookies = _client_and_cookies()
    with (
        patch.object(main, "_fetch_workspaces", return_value=WORKSPACES),
        patch.object(main, "_fetch_bucket_stats") as stats,
        patch.object(main, "_s3_client") as s3,
    ):
        resp = client.get("/", cookies=cookies)
    assert resp.status_code == 200
    stats.assert_not_called()
    s3.assert_not_called()
    html = resp.text
    # skeletons + async loaders
    assert 'id="stat-total-objects"' in html
    assert "Calculating" in html
    assert "/api/v1/storage/stats" in html
    assert "/api/pipelines" in html
    assert 'id="btn-refresh-stats"' in html
    assert 'id="pipeline-health-list"' in html


def test_home_shows_pipeline_health_not_fake_connectors():
    main, client, cookies = _client_and_cookies()
    with patch.object(main, "_fetch_workspaces", return_value=WORKSPACES):
        html = client.get("/", cookies=cookies).text
    assert "Pipeline Health" in html
    assert "Data Source Health" not in html
    for fake in ("Snowflake", "Apache Kafka", "Salesforce", "Google Analytics"):
        assert fake not in html


def test_ui_has_no_hardcoded_source_list_or_inline_scan():
    import main

    assert not hasattr(main, "SOURCES")
    assert not hasattr(main, "_get_bucket_stats")


def test_bucket_summary_sources_are_only_what_landed():
    now = datetime.now(UTC)
    objs = [
        {"Key": "workspaces/a/raw/stripe/data.parquet", "Size": 10, "LastModified": now},
        {"Key": "workspaces/a/raw/my_feed/y=1/data.parquet", "Size": 10, "LastModified": now},
        {"Key": "workspaces/a/raw/loose.txt", "Size": 1, "LastModified": now},
        {"Key": "workspaces/a/silver/t/data.parquet", "Size": 5, "LastModified": now},
    ]
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": objs}]
    res = storage_service.get_bucket_summary(s3_client=s3, workspace_slug="a")
    keys = [s["key"] for s in res["sources"]]
    assert keys == ["my_feed", "stripe"]
    assert not hasattr(storage_service, "SOURCES")


def test_empty_workspace_has_no_sources():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
    res = storage_service.get_bucket_summary(s3_client=s3, workspace_slug="empty")
    assert res["ok"] is True and res["sources"] == []
