from __future__ import annotations

import unittest.mock as mock
import uuid
from pathlib import Path

import jwt
import pytest
from starlette.testclient import TestClient

from ui.main import _WORKSPACE_COOKIE, AUTH_SECRET_KEY, app

repo_root = Path(__file__).parents[1]


@pytest.fixture
def auth_client():
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "engineer@lake.local", "role": "admin"},
        AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    client = TestClient(app)
    client.cookies.set("lake_token", token)
    client.cookies.set(_WORKSPACE_COOKIE, "ws-bundles-test")
    return client


@pytest.fixture
def mock_workspaces():
    return [
        {
            "id": "ws-bundles-test",
            "name": "DataOps Engineering",
            "slug": "dataops-eng",
            "my_role": "admin",
            "is_shared": False,
        }
    ]


# ── 1. Static Assets & Controller Verification ───────────────────────────────


def test_bundles_controller_script_and_methods():
    js_path = repo_root / "ui" / "static" / "js" / "controllers" / "bundles.js"
    assert js_path.exists(), "bundles.js controller must exist"

    content = js_path.read_text(encoding="utf-8")
    assert "bundlesManager" in content
    assert "window.openBundleDrawer" in content
    assert "window.closeBundleDrawer" in content
    assert "window.openDeployModal" in content
    assert "window.closeDeployModal" in content
    assert "window.openAIModal" in content
    assert "window.closeAIModal" in content
    assert "window.openDestroyModal" in content
    assert "window.closeDestroyModal" in content
    assert "window.validateBundleManifest" in content
    assert "window.executeBundleDeploy" in content
    assert "window.generateBundleAI" in content
    assert "window.triggerBundlePipeline" in content


def test_bundles_controller_static_route(auth_client):
    resp = auth_client.get("/static/js/controllers/bundles.js")
    assert resp.status_code == 200
    assert "bundlesManager" in resp.text


def test_base_template_includes_bundles_nav():
    base_html = (repo_root / "ui" / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'href="/bundles"' in base_html
    assert "Asset Bundles" in base_html


def test_bundles_template_elements():
    bundles_html = (repo_root / "ui" / "templates" / "bundles.html").read_text(encoding="utf-8")
    assert 'x-data="bundlesManager()"' in bundles_html
    assert "/static/js/controllers/bundles.js" in bundles_html
    assert "Lake Asset Bundles" in bundles_html
    assert "AI Generator" in bundles_html
    assert "Deploy Bundle" in bundles_html
    assert "searchQuery" in bundles_html
    assert "targetFilter" in bundles_html
    assert "statusFilter" in bundles_html
    assert "drawerOpen" in bundles_html
    assert "deployModalOpen" in bundles_html
    assert "aiModalOpen" in bundles_html
    assert "destroyModalOpen" in bundles_html


# ── 2. Page Route Tests ───────────────────────────────────────────────────────


def test_bundles_page_route(auth_client, mock_workspaces):
    with mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces):
        resp = auth_client.get("/bundles")
        assert resp.status_code == 200
        html = resp.text
        assert "Lake Asset Bundles" in html
        assert "bundlesManager" in html


# ── 3. UI API Proxy Endpoint Tests ───────────────────────────────────────────


def test_api_bundles_list_proxy(auth_client, mock_workspaces):
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"bundles":[{"bundle_name":"customer-360"}],"total":1}'

    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = mock_resp

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main.httpx.Client", return_value=mock_client),
    ):
        resp = auth_client.get("/api/bundles?target=dev&status=active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bundles"]) == 1
        assert data["bundles"][0]["bundle_name"] == "customer-360"
        bundle_calls = [c for c in mock_client.get.call_args_list if "/api/bundles" in c[0][0]]
        assert len(bundle_calls) == 1
        call_url = bundle_calls[0][0][0]
        assert "target=dev" in call_url
        assert "status=active" in call_url


def test_api_bundles_detail_proxy(auth_client, mock_workspaces):
    bundle_id = str(uuid.uuid4())
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = (
        b'{"id":"'
        + bundle_id.encode()
        + b'","bundle_name":"customer-360","drift":{"in_sync":true}}'
    )

    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value = mock_resp

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main.httpx.Client", return_value=mock_client),
    ):
        resp = auth_client.get(f"/api/bundles/{bundle_id}?include_drift=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == bundle_id
        assert data["drift"]["in_sync"] is True
        bundle_calls = [
            c for c in mock_client.get.call_args_list if f"/api/bundles/{bundle_id}" in c[0][0]
        ]
        assert len(bundle_calls) == 1


def test_api_bundles_validate_proxy(auth_client):
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"valid":true,"diff":{"to_create":["main_lake"],"to_update":[],"to_delete":[],"unchanged":[]}}'

    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp

    with mock.patch("ui.main.httpx.Client", return_value=mock_client):
        resp = auth_client.post(
            "/api/bundles/validate",
            json={
                "manifest_content": "bundle:\n  name: test\n",
                "manifest_format": "toon",
                "target": "dev",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert len(data["diff"]["to_create"]) == 1
        mock_client.post.assert_called_once()
        assert "/api/bundles/validate" in mock_client.post.call_args[0][0]


def test_api_bundles_deploy_proxy(auth_client):
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 201
    mock_resp.content = (
        b'{"bundle_name":"customer-360","target":"dev","status":"active","message":"Deployed"}'
    )

    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp

    with mock.patch("ui.main.httpx.Client", return_value=mock_client):
        resp = auth_client.post(
            "/api/bundles/deploy",
            json={
                "manifest_content": "bundle:\n  name: customer-360\n",
                "manifest_format": "toon",
                "target": "dev",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "active"
        mock_client.post.assert_called_once()
        assert "/api/bundles/deploy" in mock_client.post.call_args[0][0]


def test_api_bundles_run_proxy(auth_client):
    bundle_id = str(uuid.uuid4())
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = (
        b'{"bundle_id":"'
        + bundle_id.encode()
        + b'","pipeline_name":"orders_etl","status":"running"}'
    )

    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.return_value = mock_resp

    with mock.patch("ui.main.httpx.Client", return_value=mock_client):
        resp = auth_client.post(f"/api/bundles/{bundle_id}/run?pipeline_name=orders_etl")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        mock_client.post.assert_called_once()
        assert (
            f"/api/bundles/{bundle_id}/run?pipeline_name=orders_etl"
            in mock_client.post.call_args[0][0]
        )


def test_api_bundles_delete_proxy(auth_client):
    bundle_id = str(uuid.uuid4())
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"bundle_id":"' + bundle_id.encode() + b'","status":"destroyed"}'

    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.delete.return_value = mock_resp

    with mock.patch("ui.main.httpx.Client", return_value=mock_client):
        resp = auth_client.delete(f"/api/bundles/{bundle_id}?cascade=true&purge_storage=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "destroyed"
        mock_client.delete.assert_called_once()
        assert f"/api/bundles/{bundle_id}" in mock_client.delete.call_args[0][0]


# ── 4. AI Prompt-to-Bundle Generator Tests ────────────────────────────────────


def test_api_bundles_generate_ai_heuristic_fallback(auth_client):
    """When AI Gateway is not configured, heuristic starter generator produces valid TOON manifest."""
    with mock.patch("ui.ai_gateway_client.is_configured", return_value=False):
        resp = auth_client.post(
            "/api/bundles/generate-ai",
            json={
                "prompt": "Create a Customer 360 churn prediction lakehouse with clickstream bronze tables, aggregated gold metrics, and an executive dashboard",
                "workspace_slug": "marketing-prod",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["format"] == "toon"
        assert "customer" in data["manifest"]
        assert "bronze" in data["manifest"]
        assert "gold" in data["manifest"]
        assert "marketing-prod" in data["manifest"]


def test_api_bundles_generate_ai_with_gateway(auth_client):
    """When AI Gateway is configured, GatewayClient generates manifest."""
    fake_toon = """bundle:
  name: ecom-lake
  version: 1.0.0
targets:
  dev:
    workspace: ecom-dev
resources:
  catalogs[1]{name,description}:
    main_lake,E-commerce lakehouse
"""
    mock_gateway_res = mock.MagicMock()
    mock_gateway_res.text = fake_toon

    mock_client = mock.MagicMock()
    mock_client.models.generate_content.return_value = mock_gateway_res

    with (
        mock.patch("ui.ai_gateway_client.is_configured", return_value=True),
        mock.patch("ui.ai_gateway_client.chat_model", return_value="gemini-2.5-flash"),
        mock.patch("ui.ai_gateway_client.GatewayClient", return_value=mock_client),
    ):
        resp = auth_client.post(
            "/api/bundles/generate-ai",
            json={"prompt": "Build an ecom lake with dev target", "workspace_slug": "ecom-dev"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["model"] == "gemini-2.5-flash"
        assert "ecom-lake" in data["manifest"]


def test_bundles_controller_open_query_deep_link():
    """Verify ui/static/js/controllers/bundles.js supports ?open=<bundle_name_or_id> deep link."""
    js_content = (Path(__file__).parents[1] / "ui" / "static" / "js" / "controllers" / "bundles.js").read_text(encoding="utf-8")

    assert "urlParams.get('open')" in js_content or "urlParams.get(\"open\")" in js_content
    assert "openDrawer" in js_content
    assert "targetBundle" in js_content
