from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
ui_dir = str(repo_root / "ui")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import auth  # noqa: E402
import database  # noqa: E402
import oauth  # noqa: E402
from models import User  # noqa: E402

# Load backend app
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_auth_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_auth_test"] = backend_main
backend_spec.loader.exec_module(backend_main)
backend_client = TestClient(backend_main.app)

# Load UI app
ui_spec = importlib.util.spec_from_file_location("ui_main_auth_test", repo_root / "ui" / "main.py")
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_auth_test"] = ui_main
ui_spec.loader.exec_module(ui_main)
ui_client = TestClient(ui_main.app)


def _make_user(email: str = "test@example.com", role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name="Test User",
        role=role,
        is_active=True,
        hashed_password=auth.hash_password("ValidPassword123!"),
        created_at=datetime.now(UTC),
    )


def _make_token(user: User) -> str:
    return auth.create_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name or "",
        }
    )


# ── 1. Registration Flag Tests ────────────────────────────────────────────────


def test_is_registration_enabled():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AUTH_REGISTRATION_ENABLED", None)
        assert auth.is_registration_enabled() is True

    for val in ["true", "True", "1", "yes", "YES"]:
        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": val}):
            assert auth.is_registration_enabled() is True

    for val in ["false", "False", "0", "no", "off", "OFF"]:
        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": val}):
            assert auth.is_registration_enabled() is False


# ── 2. Register Endpoint Tests ────────────────────────────────────────────────


def test_register_allowed_when_enabled():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.count.return_value = 1  # Existing users exist

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    try:
        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": "true"}):
            resp = backend_client.post(
                "/api/auth/register",
                json={"email": "newuser@example.com", "password": "Password123!"},
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
    finally:
        backend_main.app.dependency_overrides.clear()


def test_register_blocked_when_disabled_and_existing_users():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.count.return_value = 2  # Existing users exist

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    try:
        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": "false"}):
            resp = backend_client.post(
                "/api/auth/register",
                json={"email": "newuser@example.com", "password": "Password123!"},
            )
            assert resp.status_code == 403
            assert "registration is disabled" in resp.json()["detail"].lower()
    finally:
        backend_main.app.dependency_overrides.clear()


def test_register_first_user_bootstrap_allowed_even_if_disabled():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.count.return_value = 0  # 0 users in database

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    try:
        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": "false"}):
            with patch.object(backend_main, "_create_default_workspace"):
                resp = backend_client.post(
                    "/api/auth/register",
                    json={"email": "firstadmin@example.com", "password": "Password123!"},
                )
                assert resp.status_code == 200
                assert resp.json()["ok"] is True
                assert resp.json()["role"] == "superadmin"
    finally:
        backend_main.app.dependency_overrides.clear()


# ── 3. Public Auth Config Endpoint ────────────────────────────────────────────


def test_auth_config_endpoint():
    mock_db = MagicMock()
    mock_db.query.return_value.count.return_value = 3

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    try:
        with patch.dict(
            os.environ, {"AUTH_REGISTRATION_ENABLED": "false", "GOOGLE_CLIENT_ID": "g-123"}
        ):
            resp = backend_client.get("/api/auth/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["registration_enabled"] is False
            assert any(p["key"] == "google" for p in data["providers"])
    finally:
        backend_main.app.dependency_overrides.clear()


# ── 4. Admin Auth Overview Endpoint ───────────────────────────────────────────


def test_admin_auth_overview_requires_superadmin():
    regular_user = _make_user(role="user")
    token = _make_token(regular_user)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = regular_user
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: regular_user

    try:
        resp = backend_client.get(
            "/api/admin/auth-overview",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
    finally:
        backend_main.app.dependency_overrides.clear()


def test_admin_auth_overview_superadmin_success():
    superadmin = _make_user(role="superadmin")
    token = _make_token(superadmin)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = superadmin
    mock_db.query.return_value.count.return_value = 5
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: superadmin

    env_settings = {
        "AUTH_BASE_URL": "https://lake.example.com",
        "AUTH_REGISTRATION_ENABLED": "false",
        "OKTA_CLIENT_ID": "okta-client-12345678",
        "OKTA_DOMAIN": "dev-999.okta.com",
        "GITLAB_CLIENT_ID": "gitlab-client-abcdefgh",
        "GITLAB_URL": "https://gitlab.company.internal",
    }

    try:
        with patch.dict(os.environ, env_settings):
            resp = backend_client.get(
                "/api/admin/auth-overview",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["registration_enabled"] is False
            assert data["base_url"] == "https://lake.example.com"
            assert data["user_count"] == 5

            providers_by_key = {p["key"]: p for p in data["providers"]}
            assert "okta" in providers_by_key
            assert providers_by_key["okta"]["configured"] is True
            assert providers_by_key["okta"]["client_id_masked"].startswith("okta...")
            assert (
                providers_by_key["okta"]["callback_url"]
                == "https://lake.example.com/api/auth/oauth/okta/callback"
            )

            assert "gitlab" in providers_by_key
            assert providers_by_key["gitlab"]["configured"] is True
            assert providers_by_key["gitlab"]["details"]["url"] == "https://gitlab.company.internal"
    finally:
        backend_main.app.dependency_overrides.clear()


# ── 5. OAuth Provider Config & Info Extraction ────────────────────────────────


def test_oauth_provider_config_okta():
    with patch.dict(
        os.environ,
        {
            "OKTA_DOMAIN": "mycompany.okta.com",
            "OKTA_CLIENT_ID": "okta_id",
            "OKTA_CLIENT_SECRET": "okta_secret",
        },
    ):
        cfg = oauth._provider_config("okta")
        assert cfg["auth_url"] == "https://mycompany.okta.com/oauth2/v1/authorize"
        assert cfg["token_url"] == "https://mycompany.okta.com/oauth2/v1/token"
        assert cfg["userinfo_url"] == "https://mycompany.okta.com/oauth2/v1/userinfo"
        assert cfg["client_id"] == "okta_id"
        assert cfg["client_secret"] == "okta_secret"


def test_oauth_provider_config_gitlab():
    with patch.dict(
        os.environ,
        {
            "GITLAB_URL": "https://gitlab.mycorp.com",
            "GITLAB_CLIENT_ID": "gl_id",
            "GITLAB_CLIENT_SECRET": "gl_secret",
        },
    ):
        cfg = oauth._provider_config("gitlab")
        assert cfg["auth_url"] == "https://gitlab.mycorp.com/oauth/authorize"
        assert cfg["token_url"] == "https://gitlab.mycorp.com/oauth/token"
        assert cfg["userinfo_url"] == "https://gitlab.mycorp.com/api/v4/user"
        assert cfg["client_id"] == "gl_id"


def test_oauth_provider_config_oidc_discovery():
    discovery_payload = {
        "authorization_endpoint": "https://auth.company.com/auth",
        "token_endpoint": "https://auth.company.com/token",
        "userinfo_endpoint": "https://auth.company.com/userinfo",
    }

    with patch.dict(
        os.environ,
        {
            "OIDC_ISSUER_URL": "https://auth.company.com",
            "OIDC_CLIENT_ID": "oidc_id",
            "OIDC_CLIENT_SECRET": "oidc_sec",
        },
    ):
        with patch.object(oauth, "_fetch_oidc_discovery", return_value=discovery_payload):
            cfg = oauth._provider_config("oidc")
            assert cfg["auth_url"] == "https://auth.company.com/auth"
            assert cfg["token_url"] == "https://auth.company.com/token"
            assert cfg["userinfo_url"] == "https://auth.company.com/userinfo"


def test_extract_user_info_okta_and_gitlab():
    email, name, uid = oauth._extract_user_info(
        "okta",
        {"email": "alex@okta.com", "name": "Alex Doe", "sub": "sub-123"},
        "token",
    )
    assert email == "alex@okta.com"
    assert name == "Alex Doe"
    assert uid == "sub-123"

    email, name, uid = oauth._extract_user_info(
        "gitlab",
        {"email": "sam@gitlab.com", "username": "samd", "id": 9876},
        "token",
    )
    assert email == "sam@gitlab.com"
    assert name == "samd"
    assert uid == "9876"


# ── 6. OAuth Callback Registration Disablement ────────────────────────────────


def test_oauth_callback_blocks_new_user_when_registration_disabled():
    mock_db = MagicMock()
    # No user found with this email
    mock_db.query.return_value.filter.return_value.first.return_value = None
    # 2 existing users in database (not first user)
    mock_db.query.return_value.count.return_value = 2

    mock_request = MagicMock()
    mock_request.cookies.get.return_value = "dummy-state-token"
    mock_request.query_params.get.side_effect = lambda k, default="": {
        "state": "test-state",
        "code": "auth-code-123",
    }.get(k, default)

    mock_response = MagicMock()

    with patch("jwt.decode", return_value={"state": "test-state", "next": "/"}):
        with patch.dict(
            os.environ,
            {
                "AUTH_REGISTRATION_ENABLED": "false",
                "GOOGLE_CLIENT_ID": "gid",
                "GOOGLE_CLIENT_SECRET": "gsec",
            },
        ):
            with patch("httpx.Client.post") as mock_post, patch("httpx.Client.get") as mock_get:
                mock_post.return_value.status_code = 200
                mock_post.return_value.json.return_value = {"access_token": "acc-token-123"}

                mock_get.return_value.status_code = 200
                mock_get.return_value.json.return_value = {
                    "email": "brandnew@example.com",
                    "name": "Brand New",
                    "sub": "google-sub-456",
                }

                resp = oauth.handle_oauth_callback("google", mock_request, mock_response, mock_db)
                assert resp.status_code == 307 or resp.status_code == 302
                assert "/login?error=registration_disabled" in resp.headers["location"]


# ── 7. UI Template & Rendering Tests ──────────────────────────────────────────


def test_ui_login_page_renders_with_registration_disabled():
    with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": "false"}):
        resp = ui_client.get("/login")
        assert resp.status_code == 200
        # The tabs container has the hidden class when registration is disabled
        assert "hidden" in resp.text
        assert "Sign in" in resp.text


def test_ui_settings_admin_renders_auth_overview():
    superadmin = _make_user(role="superadmin")

    auth_overview_data = {
        "registration_enabled": False,
        "base_url": "http://localhost",
        "callback_url_template": "http://localhost/api/auth/oauth/{provider}/callback",
        "user_count": 1,
        "providers": [
            {
                "key": "okta",
                "name": "Okta",
                "configured": True,
                "client_id_masked": "okta...5678",
                "details": {"domain": "dev-123.okta.com"},
                "env_vars": ["OKTA_CLIENT_ID", "OKTA_CLIENT_SECRET", "OKTA_DOMAIN"],
                "callback_url": "http://localhost/api/auth/oauth/okta/callback",
            }
        ],
    }

    with patch.object(ui_main, "_backend_get") as mock_get:

        def _side_effect(endpoint, token=None):
            if endpoint == "/api/admin/auth-overview":
                return auth_overview_data
            if endpoint == "/api/users":
                return []
            if endpoint == "/api/admin/workspaces":
                return []
            if endpoint == "/api/admin/settings":
                return {
                    "catalog_soft_delete_days": 30,
                    "workspace_inactive_grace_period_days": 30,
                    "session_inactivity_timeout_minutes": 0,
                    "session_max_hours": 24,
                }
            if endpoint == "/api/admin/ai-provider":
                return None
            return None

        mock_get.side_effect = _side_effect

        token = _make_token(superadmin)
        ui_client.cookies.set("lake_token", token)

        with patch("starlette.requests.Request.state", create=True):
            resp = ui_client.get("/settings/admin")
            assert resp.status_code == 200
            assert "Authentication &amp; SSO" in resp.text or "Authentication & SSO" in resp.text
            assert "Self-Registration Disabled" in resp.text
            assert "OAuth 2.0 / OIDC Redirect (Callback) URI" in resp.text
            assert "Okta" in resp.text
            assert "okta...5678" in resp.text
