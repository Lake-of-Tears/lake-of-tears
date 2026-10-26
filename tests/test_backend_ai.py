from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import ai_service  # noqa: E402
import auth  # noqa: E402
import database  # noqa: E402
from models import User, Workspace, WorkspaceMember  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_ai", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_ai"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def _setup_workspace_context(role: str = "admin", global_role: str = "user"):
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}@lake.local",
        display_name=role.title(),
        role=global_role,
        is_active=True,
    )
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    ws_id = uuid.uuid4()
    workspace = Workspace(
        id=ws_id,
        name="Data Science Workspace",
        slug="data-science",
        status="active",
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    mock_db = MagicMock()

    def query_side_effect(model):
        q = MagicMock()
        if model == Workspace:
            q.filter.return_value.first.return_value = workspace
            q.filter.return_value.order_by.return_value.first.return_value = workspace
        elif model == WorkspaceMember:
            q.filter.return_value.first.return_value = member
        else:
            q.filter.return_value.order_by.return_value.all.return_value = []
        return q

    mock_db.query.side_effect = query_side_effect
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user
    return user, workspace, token


def test_backend_ai_context_cli_headers():
    """Verify GET /api/v1/ai/context resolves workspace via X-Workspace-Slug and returns SQL AI functions."""
    _, workspace, token = _setup_workspace_context(role="admin")
    try:
        with patch.object(ai_service, "get_workspace_catalogs_from_db", return_value=[]):
            resp = client.get(
                "/api/v1/ai/context",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Workspace-Slug": workspace.slug,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["workspace_slug"] == "data-science"
            fn_names = [f["name"] for f in data["sql_ai_functions"]]
            assert fn_names == ["ai_query", "ai_summarize", "ai_classify", "ai_extract"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_ai_sql_execute_and_read_only_guardrail():
    """Verify POST /api/v1/ai/sql/execute runs DuckDB queries and enforces read_only mode when requested."""
    _, workspace, token = _setup_workspace_context(role="viewer")
    try:
        with patch.object(ai_service, "get_workspace_catalogs_from_db", return_value=[]):
            # 1. Valid SELECT query
            resp = client.post(
                "/api/v1/ai/sql/execute",
                headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
                json={"sql": "SELECT 100 AS metric, 'active' AS state", "read_only": True},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["columns"] == ["metric", "state"]
            assert data["rows"] == [[100, "active"]]

            # 2. Destructive query blocked when read_only=True
            resp2 = client.post(
                "/api/v1/ai/sql/execute",
                headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
                json={"sql": "DROP TABLE users", "read_only": True},
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["ok"] is False
            assert "Only read-only queries" in data2["error"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_ai_sql_generate_fix_and_explain():
    """Verify POST /api/v1/ai/sql/generate, /fix, and /explain on the backend router."""
    _, workspace, token = _setup_workspace_context(role="admin")
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(
        {
            "sql": "SELECT ai_summarize(content) FROM read_parquet('s3://datalake/embeddings/**/*.parquet') LIMIT 5",
            "fixed_sql": "SELECT ai_summarize(content) FROM read_parquet('s3://datalake/embeddings/**/*.parquet') LIMIT 5",
            "explanation": "Uses ai_summarize UDF on embeddings.",
            "summary": "Summarizes top 5 embeddings rows.",
            "steps": ["Scan embeddings parquet", "Apply ai_summarize UDF"],
            "tables_used": ["embeddings"],
            "tables_referenced": ["embeddings"],
            "ai_functions_used": ["ai_summarize"],
        }
    )
    mock_client.models.generate_content.return_value = mock_resp

    try:
        with (
            patch.object(ai_service, "get_workspace_catalogs_from_db", return_value=[]),
            patch.object(ai_service, "_resolve_api_key", return_value="provider"),
            patch.object(ai_service, "_client_for", return_value=mock_client),
        ):
            headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug}

            gen_resp = client.post(
                "/api/v1/ai/sql/generate",
                headers=headers,
                json={"prompt": "Summarize top 5 embeddings"},
            )
            assert gen_resp.status_code == 200
            assert gen_resp.json()["ok"] is True
            assert "ai_summarize" in gen_resp.json()["sql"]

            fix_resp = client.post(
                "/api/v1/ai/sql/fix",
                headers=headers,
                json={"sql": "SELECT bad_col FROM embeddings", "error": "Column bad_col not found"},
            )
            assert fix_resp.status_code == 200
            assert fix_resp.json()["ok"] is True
            assert "ai_summarize" in fix_resp.json()["fixed_sql"]

            exp_resp = client.post(
                "/api/v1/ai/sql/explain",
                headers=headers,
                json={"sql": "SELECT ai_summarize(content) FROM embeddings LIMIT 5"},
            )
            assert exp_resp.status_code == 200
            assert exp_resp.json()["ok"] is True
            assert exp_resp.json()["ai_functions_used"] == ["ai_summarize"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_secret_encryption_and_masking():
    """Verify encrypt_secret, decrypt_secret, tamper detection, and mask_secret."""
    raw = "AIzaSyTestSecretKey1234567890ABCD"
    encrypted = auth.encrypt_secret(raw)
    assert encrypted.startswith("v1:")
    assert raw not in encrypted
    assert auth.decrypt_secret(encrypted) == raw
    assert auth.mask_secret(raw) == "••••••••ABCD"
    assert auth.mask_secret("1234") == "••••••••"
    assert auth.mask_secret("") is None

    # Tampered MAC tag must fail decryption cleanly
    parts = encrypted.split(":")
    tampered = f"{parts[0]}:{parts[1]}:{parts[2]}:AAAA"
    assert auth.decrypt_secret(tampered) == ""


def test_admin_settings_gemini_api_key_lifecycle_and_internal_endpoint():
    """Verify superadmin can set, mask, test, and clear GEMINI_API_KEY in /api/admin/settings."""
    from models import SystemSetting

    _, _, token = _setup_workspace_context(role="admin", global_role="superadmin")
    mock_db = MagicMock()
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    settings_store: dict[str, SystemSetting] = {}

    def query_side_effect(model):
        q = MagicMock()
        if model == SystemSetting:

            def filter_side_effect(*args, **kwargs):
                fq = MagicMock()
                right_val = (
                    getattr(getattr(args[0], "right", None), "value", None) if args else None
                )
                if right_val in ("gemini_api_key", "gemini_default_model"):
                    fq.first.return_value = settings_store.get(right_val)
                else:
                    fq.first.return_value = None
                return fq

            q.filter.side_effect = filter_side_effect
        return q

    def add_side_effect(obj):
        if isinstance(obj, SystemSetting):
            settings_store[obj.key] = obj

    mock_db.query.side_effect = query_side_effect
    mock_db.add.side_effect = add_side_effect

    try:
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Initially not configured
        get_resp = client.get("/api/admin/settings", headers=headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["gemini_api_key_configured"] is False
        assert get_resp.json()["gemini_api_key_masked"] is None

        # 2. Save GEMINI_API_KEY via PATCH /api/admin/settings
        raw_key = "AIzaSySuperSecretKey9876"
        patch_resp = client.patch(
            "/api/admin/settings",
            headers=headers,
            json={"gemini_api_key": raw_key},
        )
        assert patch_resp.status_code == 200
        data = patch_resp.json()
        assert data["gemini_api_key_configured"] is True
        assert data["gemini_api_key_masked"] == "••••••••9876"
        assert raw_key not in json.dumps(data)

        # Verify stored encrypted at rest
        stored_obj = settings_store.get("gemini_api_key")
        assert stored_obj is not None
        assert stored_obj.value_str.startswith("v1:")
        assert raw_key not in stored_obj.value_str
        assert auth.get_stored_gemini_api_key(mock_db) == raw_key

        # 3. Internal endpoint returns decrypted key when authenticated with X-Internal-Secret
        int_resp = client.get(
            "/api/internal/settings/gemini-key",
            headers={"X-Internal-Secret": auth.SECRET_KEY},
        )
        assert int_resp.status_code == 200
        assert int_resp.json() == {
            "gemini_api_key": raw_key,
            "configured": True,
            "gemini_default_model": "gemini-3.8-flash",
        }

        # 3b. Default model can be set via the Admin Panel and is exposed to internal services
        model_resp = client.patch(
            "/api/admin/settings",
            headers=headers,
            json={"gemini_default_model": "gemini-custom-model"},
        )
        assert model_resp.status_code == 200
        assert model_resp.json()["gemini_default_model"] == "gemini-custom-model"
        int_resp2 = client.get(
            "/api/internal/settings/gemini-key",
            headers={"X-Internal-Secret": auth.SECRET_KEY},
        )
        assert int_resp2.json()["gemini_default_model"] == "gemini-custom-model"
        reset_resp = client.patch(
            "/api/admin/settings", headers=headers, json={"gemini_default_model": ""}
        )
        assert reset_resp.json()["gemini_default_model"] == "gemini-3.8-flash"

        # Unauthorized internal call rejected
        unauth_resp = client.get(
            "/api/internal/settings/gemini-key",
            headers={"X-Internal-Secret": "wrong-secret"},
        )
        assert unauth_resp.status_code == 401

        # 4. Test key endpoint
        with patch.object(
            ai_service,
            "test_gemini_api_key",
            return_value={
                "ok": True,
                "model": "gemini-3.8-flash",
                "message": "Connected to gemini-3.8-flash successfully (OK).",
            },
        ) as mock_test:
            test_resp = client.post(
                "/api/admin/settings/test-gemini-key",
                headers=headers,
                json={},
            )
            assert test_resp.status_code == 200
            assert test_resp.json()["ok"] is True
            mock_test.assert_called_once_with(api_key=raw_key, model="gemini-3.8-flash")

        # 5. Clear GEMINI_API_KEY
        clear_resp = client.patch(
            "/api/admin/settings",
            headers=headers,
            json={"clear_gemini_api_key": True},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["gemini_api_key_configured"] is False
        assert clear_resp.json()["gemini_api_key_masked"] is None
        assert auth.get_stored_gemini_api_key(mock_db) == ""
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_ai_chat_and_cancellation():
    """Verify POST /api/v1/ai/chat handles cancellation cleanly and supports /chat/cancel."""
    import inspect

    from ai_router import (
        cancel_genie_chat,
        execute_sql,
        explain_sql,
        fix_sql,
        generate_sql,
        genie_chat,
        get_ai_context,
    )

    # Verify all AI endpoints are non-coroutine defs to prevent event loop blocking
    for fn in (
        get_ai_context,
        execute_sql,
        generate_sql,
        fix_sql,
        explain_sql,
        genie_chat,
        cancel_genie_chat,
    ):
        assert not inspect.iscoroutinefunction(
            fn
        ), f"{fn.__name__} must be sync def to run in threadpool"

    _, workspace, token = _setup_workspace_context(role="admin")
    try:
        # Test cancel endpoint acknowledges cancellation
        cancel_resp = client.post(
            "/api/v1/ai/chat/cancel",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json={"request_id": "test-req-123"},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json() == {"ok": True, "cancelled": True}

        # Test chat invocation with cancel token passed to service
        with (
            patch.object(ai_service, "get_workspace_catalogs_from_db", return_value=[]),
            patch.object(
                ai_service,
                "run_genie_chat",
                return_value={
                    "ok": False,
                    "mode": "genie",
                    "cancelled": True,
                    "error": "Request cancelled",
                },
            ) as mock_genie,
        ):
            chat_resp = client.post(
                "/api/v1/ai/chat",
                headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
                json={"question": "Test query", "request_id": "req-cancelled-1"},
            )
            assert chat_resp.status_code == 200
            data = chat_resp.json()
            assert data["ok"] is False
            assert data["cancelled"] is True
            assert mock_genie.call_count == 1
            call_kwargs = mock_genie.call_args.kwargs
            assert call_kwargs["cancel"] is not None
    finally:
        backend_main.app.dependency_overrides.clear()
