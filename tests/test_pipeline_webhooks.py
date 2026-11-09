from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import pipeline_service  # noqa: E402
from bundles.deployer import generate_bundle_dag_code  # noqa: E402
from bundles.parser import parse_bundle_str  # noqa: E402
from models import (  # noqa: E402
    PipelineMetadata,
    User,
    Workspace,
    WorkspaceMember,
    generate_webhook_secret,
)

# Load backend app
spec = importlib.util.spec_from_file_location(
    "backend_main_pipeline_webhooks", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_pipeline_webhooks"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def _setup_test_context(webhook_enabled: bool = True):
    ws_id = uuid.uuid4()
    user_id = uuid.uuid4()
    secret = generate_webhook_secret()
    dag_id = "tmd_291_email_pipeline"

    workspace = Workspace(
        id=ws_id,
        name="Operations",
        slug="operations",
        status="active",
        webhook_secret=secret,
        deleted_at=None,
    )
    user = User(
        id=user_id,
        email="operator@lake.local",
        display_name="Operator",
        role="user",
        is_active=True,
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role="admin",
    )
    workspace.members = [member]

    meta = PipelineMetadata(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        dag_id=dag_id,
        display_label="TMD 291 Email Pipeline",
        description="Daily & Webhook Ingest",
        color_hex="#10B981",
        is_visible_on_timeline=True,
        webhook_enabled=webhook_enabled,
    )

    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    return workspace, user, token, secret, dag_id, meta


@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    backend_main.app.dependency_overrides.clear()


# ── Native Pipeline Webhook Tests (Issue #433) ────────────────────────────────


def test_pipeline_webhook_microsoft_graph_handshake():
    """Verify Microsoft Graph subscription validation echoes plaintext token."""
    resp = client.get(
        "/api/v1/pipelines/operations/tmd_291_email_pipeline/webhook?validationToken=msft_test_token_123"
    )
    assert resp.status_code == 200
    assert resp.text == "msft_test_token_123"
    assert "text/plain" in resp.headers["content-type"]

    # Also supports POST validation
    resp_post = client.post(
        "/api/v1/pipelines/operations/tmd_291_email_pipeline/webhook?validationToken=msft_post_token_456"
    )
    assert resp_post.status_code == 200
    assert resp_post.text == "msft_post_token_456"


def test_pipeline_webhook_triggered_with_secret_header():
    """Verify webhook execution triggered via X-Lake-Webhook-Secret header."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=True)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [workspace, meta]
    mock_db.query.return_value.filter.return_value.all.return_value = [meta]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    payload = {"source": "ms_graph", "change": "new_email", "message_id": "MSG_999"}

    with patch.object(pipeline_service, "trigger_dag_run", new_callable=AsyncMock) as mock_trigger:
        mock_trigger.return_value = {
            "dag_id": dag_id,
            "dag_run_id": "webhook__20261108T230000Z__test123",
            "state": "queued",
            "execution_date": "2026-11-08T23:00:00Z",
        }

        resp = client.post(
            f"/api/v1/pipelines/operations/{dag_id}/webhook",
            json=payload,
            headers={"X-Lake-Webhook-Secret": secret, "X-Custom-Test": "Passed"},
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "queued"
        assert data["dag_id"] == dag_id
        assert data["dag_run_id"] == "webhook__20261108T230000Z__test123"

        mock_trigger.assert_called_once()
        call_kwargs = mock_trigger.call_args[1]
        assert call_kwargs["workspace_slug"] == "operations"
        assert call_kwargs["dag_id"] == dag_id
        assert call_kwargs["triggered_by"] == "webhook"
        conf = call_kwargs["conf"]
        assert conf["webhook_payload"] == payload
        assert "x-custom-test" in conf["webhook_headers"]


def test_pipeline_webhook_triggered_with_query_token():
    """Verify webhook execution triggered via ?token=... query parameter."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=True)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [workspace, meta]
    mock_db.query.return_value.filter.return_value.all.return_value = [meta]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    with patch.object(pipeline_service, "trigger_dag_run", new_callable=AsyncMock) as mock_trigger:
        mock_trigger.return_value = {
            "dag_id": dag_id,
            "dag_run_id": "webhook__20261108T230000Z__test456",
            "state": "queued",
        }

        resp = client.post(
            f"/api/v1/pipelines/operations/{dag_id}/webhook?token={secret}",
            json={"event": "invoice_received"},
        )

        assert resp.status_code == 201
        assert resp.json()["status"] == "queued"
        mock_trigger.assert_called_once()


def test_pipeline_webhook_hmac_sha256_signature():
    """Verify HMAC-SHA256 signature verification (GitHub & Stripe format)."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=True)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [workspace, meta]
    mock_db.query.return_value.filter.return_value.all.return_value = [meta]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    payload_bytes = json.dumps({"action": "push", "ref": "refs/heads/main"}).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    with patch.object(pipeline_service, "trigger_dag_run", new_callable=AsyncMock) as mock_trigger:
        mock_trigger.return_value = {
            "dag_id": dag_id,
            "dag_run_id": "webhook__sig123",
            "state": "queued",
        }

        resp = client.post(
            f"/api/v1/pipelines/operations/{dag_id}/webhook",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
        assert resp.status_code == 201


def test_pipeline_webhook_unwraps_gcp_pubsub():
    """Verify that GCP Pub/Sub push messages are unwrapped from base64 data."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=True)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [workspace, meta]
    mock_db.query.return_value.filter.return_value.all.return_value = [meta]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    inner_data = {"emailAddress": "accounting@olanderlee.com", "historyId": "987654"}
    b64_data = base64.b64encode(json.dumps(inner_data).encode("utf-8")).decode("utf-8")
    pubsub_envelope = {
        "message": {
            "data": b64_data,
            "messageId": "pubsub_12345",
            "publishTime": "2026-11-08T23:00:00Z",
        },
        "subscription": "projects/my-gcp-proj/subscriptions/gmail-watch",
    }

    with patch.object(pipeline_service, "trigger_dag_run", new_callable=AsyncMock) as mock_trigger:
        mock_trigger.return_value = {
            "dag_id": dag_id,
            "dag_run_id": "webhook__pubsub_run",
            "state": "queued",
        }

        resp = client.post(
            f"/api/v1/pipelines/operations/{dag_id}/webhook",
            json=pubsub_envelope,
            headers={"X-Lake-Webhook-Secret": secret},
        )

        assert resp.status_code == 201
        conf = mock_trigger.call_args[1]["conf"]
        assert conf["webhook_payload"]["unwrapped_data"] == inner_data


def test_pipeline_webhook_rejects_invalid_secret():
    """Verify unauthorized rejection when webhook secret is missing or incorrect."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=True)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = workspace
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    resp = client.post(
        f"/api/v1/pipelines/operations/{dag_id}/webhook",
        json={"test": "unauthorized"},
        headers={"X-Lake-Webhook-Secret": "whsec_wrong_secret"},
    )
    assert resp.status_code == 401
    assert "Invalid or missing" in resp.json()["detail"]


def test_pipeline_webhook_rejects_when_disabled():
    """Verify 403 Forbidden rejection when pipeline has webhook_enabled=False."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=False)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [workspace, meta]
    mock_db.query.return_value.filter.return_value.all.return_value = [meta]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    resp = client.post(
        f"/api/v1/pipelines/operations/{dag_id}/webhook",
        json={"test": "payload"},
        headers={"X-Lake-Webhook-Secret": secret},
    )
    assert resp.status_code == 403
    assert "Webhook trigger is disabled" in resp.json()["detail"]


def test_pipeline_settings_webhook_toggle_and_url():
    """Verify updating webhook_enabled via settings API and URL generation."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=False)

    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        workspace,
        workspace.members[0],
        meta,
    ]
    mock_db.query.return_value.filter.return_value.all.return_value = [meta]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    # 1. Get Settings
    resp_get = client.get(
        f"/api/pipelines/{dag_id}/settings?workspace_slug=operations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_get.status_code == 200
    data = resp_get.json()
    assert data["webhook_enabled"] is False
    assert data["webhook_url"] == f"/api/v1/pipelines/operations/{dag_id}/webhook"

    # 2. Update webhook_enabled to True
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        workspace,
        workspace.members[0],
        meta,
    ]
    resp_patch = client.patch(
        f"/api/pipelines/{dag_id}/settings?workspace_slug=operations",
        json={"webhook_enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_patch.status_code == 200
    assert resp_patch.json()["webhook_enabled"] is True


# ── Ingest Webhook Direct Trigger Tests (Issue #434) ──────────────────────────


def test_ingest_webhook_with_direct_trigger():
    """Verify /api/v1/ingest/webhooks/{slug}?trigger_pipeline=... buffers to S3 and fires pipeline."""
    workspace, user, token, secret, dag_id, meta = _setup_test_context(webhook_enabled=True)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = workspace
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    mock_s3 = MagicMock()
    with patch("webhooks_router.get_s3_client", return_value=mock_s3):
        with patch.object(
            pipeline_service, "trigger_dag_run", new_callable=AsyncMock
        ) as mock_trigger:
            mock_trigger.return_value = {
                "dag_id": dag_id,
                "dag_run_id": "ingest__run_777",
                "state": "queued",
            }

            resp = client.post(
                f"/api/v1/ingest/webhooks/operations?trigger_pipeline={dag_id}",
                json={"invoice_num": "INV-101", "amount": 250.0},
                headers={"X-Lake-Webhook-Secret": secret},
            )

            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "accepted"
            assert data["triggered_pipeline"] == dag_id
            assert data["dag_run_id"] == "ingest__run_777"

            # Check that S3 object was stored
            mock_s3.put_object.assert_called_once()
            # Check pipeline trigger
            mock_trigger.assert_called_once()
            conf = mock_trigger.call_args[1]["conf"]
            assert conf["triggered_by"] == "webhook_ingest"
            assert (
                "s3://datalake/workspaces/operations/raw/webhook/buffer" in conf["buffered_s3_uri"]
            )


# ── LAB Bundle Manifest & Virtualenv Support Tests (Issue #435) ────────────────


def test_bundle_manifest_webhook_and_requirements():
    """Verify lake.toon parser reads webhook_enabled and task requirements."""
    toon_manifest = """
bundle:
  name: tmd-291-store-pipeline
  version: 1.0.0

targets:
  dev:
    workspace: operations

resources:
  pipelines:
    tmd_291_email_pipeline:
      description: Email pipeline with webhooks and requirements
      schedule: "@webhook"
      webhook_enabled: true
      tasks[2]{name,type,entrypoint,requirements,upstream_tasks}:
        fetch_email_streams,python_script,tasks/fetch_email_streams.py,tasks/requirements.txt,[]
        stage_silver,duckdb_sql,tasks/stage_silver.sql,,[fetch_email_streams]
"""
    spec = parse_bundle_str(toon_manifest, format="toon")
    pipe = spec.resources.pipelines[0]
    assert pipe.name == "tmd_291_email_pipeline"
    assert pipe.webhook_enabled is True
    assert pipe.schedule is None  # Normalized from @webhook

    task1 = pipe.tasks[0]
    assert task1.name == "fetch_email_streams"
    assert task1.requirements == "tasks/requirements.txt"

    # Verify DAG code generation includes requirements in op_kwargs
    dag_id, code = generate_bundle_dag_code(
        workspace_slug="operations",
        bundle_name="tmd-291-store-pipeline",
        target_name="dev",
        pipeline=pipe,
    )
    assert dag_id == "ws_operations_tmd_291_store_pipeline_tmd_291_email_pipeline"
    assert '"requirements": "tasks/requirements.txt"' in code
