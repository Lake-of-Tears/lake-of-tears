from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import sys
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
from models import User, Workspace, WorkspaceMember, generate_webhook_secret  # noqa: E402

# Load backend app
spec = importlib.util.spec_from_file_location(
    "backend_main_webhooks", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_webhooks"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _create_test_workspace_and_user(role="admin", global_role="user"):
    ws_id = uuid.uuid4()
    user_id = uuid.uuid4()
    secret = f"whsec_{uuid.uuid4().hex}"

    workspace = Workspace(
        id=ws_id,
        name="Engineering",
        slug="engineering",
        status="active",
        webhook_secret=secret,
        deleted_at=None,
    )
    user = User(
        id=user_id,
        email="dev@lake.local",
        display_name="Engineer",
        role=global_role,
        is_active=True,
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    workspace.members = [member]

    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    return workspace, user, token, secret


@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    backend_main.app.dependency_overrides.clear()


# ── Test Suite ────────────────────────────────────────────────────────────────


def test_generate_webhook_secret_format():
    sec = generate_webhook_secret()
    assert sec.startswith("whsec_")
    assert len(sec) > 30


def test_get_webhook_secret_authenticated():
    workspace, user, token, secret = _create_test_workspace_and_user(role="member")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        workspace,
        workspace.members[0],
    ]

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    resp = client.get(
        f"/api/v1/workspaces/{workspace.slug}/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_slug"] == "engineering"
    assert data["webhook_secret"] == secret
    assert data["webhook_url"] == "/api/v1/ingest/webhooks/engineering"


def test_regenerate_webhook_secret_admin_allowed():
    workspace, user, token, secret = _create_test_workspace_and_user(role="admin")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        workspace,
        workspace.members[0],
    ]

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    resp = client.post(
        f"/api/v1/workspaces/{workspace.slug}/webhook-secret/regenerate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhook_secret"].startswith("whsec_")
    assert data["webhook_secret"] != secret


def test_webhook_receiver_token_header_auth():
    workspace, user, token, secret = _create_test_workspace_and_user()
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = workspace
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    with patch("webhooks_router.get_s3_client") as mock_s3:
        s3_client = MagicMock()
        mock_s3.return_value = s3_client

        payload = {"event": "order.completed", "amount": 149.50}
        resp = client.post(
            "/api/v1/ingest/webhooks/engineering",
            json=payload,
            headers={"X-Lake-Webhook-Secret": secret},
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["workspace_slug"] == "engineering"
        assert "event_id" in data

        # Verify S3 buffer put_object
        assert s3_client.put_object.called
        call_kwargs = s3_client.put_object.call_args[1]
        assert "raw/webhook/buffer" in call_kwargs["Key"]
        assert call_kwargs["Key"].endswith(".parquet")

        # Verify Parquet payload content
        buf = io.BytesIO(call_kwargs["Body"])
        table = pq.read_table(buf)
        assert table.num_rows == 1
        assert "order.completed" in table.column("payload")[0].as_py()
        assert table.column("source")[0].as_py() == "webhook"


def test_webhook_receiver_hmac_signature_auth():
    workspace, user, token, secret = _create_test_workspace_and_user()
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = workspace
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    body_bytes = b'{"event": "payment.succeeded", "charge_id": "ch_12345"}'
    signature = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

    with patch("webhooks_router.get_s3_client") as mock_s3:
        s3_client = MagicMock()
        mock_s3.return_value = s3_client

        resp = client.post(
            "/api/v1/ingest/webhooks/engineering",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Lake-Signature": f"sha256={signature}",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"


def test_webhook_receiver_query_token_auth():
    workspace, user, token, secret = _create_test_workspace_and_user()
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = workspace
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    with patch("webhooks_router.get_s3_client") as mock_s3:
        s3_client = MagicMock()
        mock_s3.return_value = s3_client

        resp = client.post(
            f"/api/v1/ingest/webhooks/engineering?token={secret}",
            json={"data": "test_query_token"},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"


def test_webhook_receiver_rejects_missing_or_invalid_secret():
    workspace, user, token, secret = _create_test_workspace_and_user()
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = workspace
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    # Missing secret
    resp = client.post(
        "/api/v1/ingest/webhooks/engineering",
        json={"foo": "bar"},
    )
    assert resp.status_code == 401

    # Invalid secret
    resp2 = client.post(
        "/api/v1/ingest/webhooks/engineering",
        json={"foo": "bar"},
        headers={"X-Lake-Webhook-Secret": "whsec_wrongsecret"},
    )
    assert resp2.status_code == 401


def test_webhook_receiver_404_on_unknown_workspace():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    resp = client.post(
        "/api/v1/ingest/webhooks/nonexistent-workspace",
        json={"foo": "bar"},
        headers={"X-Lake-Webhook-Secret": "whsec_any"},
    )
    assert resp.status_code == 404


def test_ingest_webhook_dag_structure(monkeypatch):
    """Verify ingest_webhook Airflow DAG definition, task IDs, and dependencies."""
    airflow = types.ModuleType("airflow")
    airflow.DAG = MagicMock()
    operators = types.ModuleType("airflow.operators")
    python = types.ModuleType("airflow.operators.python")
    python.PythonOperator = MagicMock()
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.operators", operators)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", python)

    dag_path = Path(__file__).parents[1] / "pipeline" / "dags" / "ingest_webhook_dag.py"
    spec = importlib.util.spec_from_file_location("ingest_webhook_dag", dag_path)
    dag_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dag_mod)

    assert hasattr(dag_mod, "compact_webhook_buffers")
    assert hasattr(dag_mod, "register_catalog_tables")
    assert hasattr(dag_mod, "WEBHOOK_COLUMNS")
    assert len(dag_mod.WEBHOOK_COLUMNS) == 5


def test_ingest_webhook_compaction_logic(monkeypatch):
    """Verify compact_webhook_buffers combines buffer files and purges them."""
    airflow = types.ModuleType("airflow")
    airflow.DAG = MagicMock()
    operators = types.ModuleType("airflow.operators")
    python = types.ModuleType("airflow.operators.python")
    python.PythonOperator = MagicMock()
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.operators", operators)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", python)

    dag_path = Path(__file__).parents[1] / "pipeline" / "dags" / "ingest_webhook_dag.py"
    spec = importlib.util.spec_from_file_location("ingest_webhook_dag_test", dag_path)
    dag_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dag_mod)

    # Create dummy buffer table
    table = pa.Table.from_pydict(
        {
            "event_id": ["evt-1"],
            "received_at": [datetime.now(UTC)],
            "source": ["webhook"],
            "headers": ["{}"],
            "payload": ['{"test": true}'],
        },
        schema=pa.schema(
            [
                ("event_id", pa.string()),
                ("received_at", pa.timestamp("us", tz="UTC")),
                ("source", pa.string()),
                ("headers", pa.string()),
                ("payload", pa.string()),
            ]
        ),
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    parquet_bytes = buf.getvalue()

    mock_s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Contents": [
                {
                    "Key": "workspaces/analytics/raw/webhook/buffer/year=2026/month=11/day=02/evt-1.parquet"
                }
            ]
        }
    ]
    mock_s3.get_paginator.return_value = paginator
    mock_s3.get_object.return_value = {"Body": io.BytesIO(parquet_bytes)}

    monkeypatch.setattr(dag_mod, "_get_s3_client", lambda: mock_s3)

    mock_ti = MagicMock()
    context = {
        "dag_run": MagicMock(conf={"workspace_slug": "analytics"}),
        "ti": mock_ti,
    }

    processed = dag_mod.compact_webhook_buffers(**context)
    assert processed == ["analytics"]

    # Verify write of compacted file
    assert mock_s3.put_object.called
    put_key = mock_s3.put_object.call_args[1]["Key"]
    assert "workspaces/analytics/raw/webhook/year=2026/month=11/day=02/compacted_" in put_key

    # Verify deletion of raw buffer file
    assert mock_s3.delete_objects.called
    del_objs = mock_s3.delete_objects.call_args[1]["Delete"]["Objects"]
    assert any(o["Key"].endswith("evt-1.parquet") for o in del_objs)


def test_ingest_webhook_catalog_registration(monkeypatch):
    """Verify register_catalog_tables inserts or updates catalog_tables."""
    airflow = types.ModuleType("airflow")
    airflow.DAG = MagicMock()
    operators = types.ModuleType("airflow.operators")
    python = types.ModuleType("airflow.operators.python")
    python.PythonOperator = MagicMock()
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.operators", operators)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", python)

    dag_path = Path(__file__).parents[1] / "pipeline" / "dags" / "ingest_webhook_dag.py"
    spec = importlib.util.spec_from_file_location("ingest_webhook_dag_cat", dag_path)
    dag_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dag_mod)

    mock_db = MagicMock()
    ws_uuid = uuid.uuid4()
    cat_uuid = uuid.uuid4()
    schema_uuid = uuid.uuid4()

    # Mock execute results for:
    # 1. ws lookup
    # 2. catalog lookup
    # 3. schema lookup
    # 4. table lookup (None -> triggers INSERT)
    mock_db.execute.return_value.first.side_effect = [
        (ws_uuid,),
        (cat_uuid,),
        (schema_uuid,),
        None,
    ]

    monkeypatch.setattr(dag_mod, "_get_db", lambda: mock_db)

    mock_ti = MagicMock()
    mock_ti.xcom_pull.return_value = ["analytics"]
    context = {"ti": mock_ti}

    dag_mod.register_catalog_tables(**context)

    assert mock_db.commit.called
    # Check that INSERT into catalog_tables occurred
    executed_queries = [str(call[0][0]) for call in mock_db.execute.call_args_list]
    assert any("INSERT INTO catalog_tables" in q for q in executed_queries)
