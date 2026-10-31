from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
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


# ── 8. Provider On/Off Toggles & Apple Sign In ─────────────────────────────────


def test_provider_enabled_toggles():
    # Unset toggle falls back to clientId presence
    with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "gid"}, clear=True):
        assert oauth.is_provider_enabled("google") is True

    # Truthy toggles
    for val in ["true", "True", "1", "yes", "YES"]:
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "gid", "GOOGLE_ENABLED": val}):
            assert oauth.is_provider_enabled("google") is True

    # Falsy toggles
    for val in ["false", "False", "0", "no", "off"]:
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "gid", "GOOGLE_ENABLED": val}):
            assert oauth.is_provider_enabled("google") is False

    # Apple requires all 4 credentials
    with patch.dict(
        os.environ,
        {
            "APPLE_CLIENT_ID": "com.lake.service",
            "APPLE_TEAM_ID": "TEAM123",
            "APPLE_KEY_ID": "KEY123",
            "APPLE_PRIVATE_KEY": "fake-key",
            "APPLE_ENABLED": "true",
        },
    ):
        assert oauth.is_provider_enabled("apple") is True

    # Apple missing team_id is disabled even if enabled=true
    with patch.dict(
        os.environ,
        {
            "APPLE_CLIENT_ID": "com.lake.service",
            "APPLE_KEY_ID": "KEY123",
            "APPLE_PRIVATE_KEY": "fake-key",
            "APPLE_ENABLED": "true",
        },
        clear=True,
    ):
        assert oauth.is_provider_enabled("apple") is False


def test_enabled_providers_list():
    env = {
        "GOOGLE_CLIENT_ID": "gid",
        "GOOGLE_ENABLED": "true",
        "GITHUB_CLIENT_ID": "ghid",
        "GITHUB_ENABLED": "false",
        "APPLE_CLIENT_ID": "com.lake.service",
        "APPLE_TEAM_ID": "TEAM123",
        "APPLE_KEY_ID": "KEY123",
        "APPLE_PRIVATE_KEY": "fake-key",
        "APPLE_ENABLED": "true",
    }
    with patch.dict(os.environ, env, clear=True):
        providers = oauth.enabled_providers()
        keys = [p["key"] for p in providers]
        assert "google" in keys
        assert "apple" in keys
        assert "github" not in keys
        apple_item = next(p for p in providers if p["key"] == "apple")
        assert apple_item["label"] == "Apple"


def test_apple_key_normalization_and_secret_generation():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pk = ec.generate_private_key(ec.SECP256R1())
    raw_pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    # Test key normalization with literal escaped \n
    escaped_pem = raw_pem.replace("\n", "\\n")
    norm1 = oauth._normalize_private_key(escaped_pem)
    assert "-----BEGIN PRIVATE KEY-----" in norm1
    assert "\n" in norm1

    # Test key normalization with base64
    import base64

    b64_pem = base64.b64encode(raw_pem.encode("utf-8")).decode("utf-8")
    norm2 = oauth._normalize_private_key(b64_pem)
    assert "-----BEGIN PRIVATE KEY-----" in norm2

    # Test ES256 client secret JWT generation
    secret_jwt = oauth._generate_apple_client_secret(
        client_id="com.lake.service",
        team_id="TEAM123456",
        key_id="KEY1234567",
        private_key=raw_pem,
    )
    assert isinstance(secret_jwt, str)
    unverified_headers = jwt.get_unverified_header(secret_jwt)
    assert unverified_headers["alg"] == "ES256"
    assert unverified_headers["kid"] == "KEY1234567"

    unverified_claims = jwt.decode(secret_jwt, options={"verify_signature": False})
    assert unverified_claims["iss"] == "TEAM123456"
    assert unverified_claims["sub"] == "com.lake.service"
    assert unverified_claims["aud"] == "https://appleid.apple.com"
    assert unverified_claims["exp"] > unverified_claims["iat"]


def test_apple_oauth_callback_flow():
    import json

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.count.return_value = 1

    # Fake Apple id_token
    dummy_apple_id_token = jwt.encode(
        {"email": "appleuser@privaterelay.appleid.com", "sub": "apple-sub-123456"},
        "dummy-secret",
        algorithm="HS256",
    )

    mock_request = MagicMock()
    mock_request.cookies.get.return_value = "dummy-state-token"
    mock_request.query_params.get.side_effect = lambda k, default="": {
        "state": "test-state",
    }.get(k, default)

    mock_response = MagicMock()

    form_payload = {
        "code": "apple-auth-code-789",
        "state": "test-state",
        "user": json.dumps({"name": {"firstName": "Johnny", "lastName": "Appleseed"}}),
    }

    env_settings = {
        "APPLE_CLIENT_ID": "com.lake.service",
        "APPLE_TEAM_ID": "TEAM123456",
        "APPLE_KEY_ID": "KEY1234567",
        "APPLE_PRIVATE_KEY": "dummy_pk",
        "APPLE_ENABLED": "true",
    }

    with patch(
        "jwt.decode",
        side_effect=lambda token, *args, **kwargs: (
            {"state": "test-state", "next": "/catalog"}
            if token == "dummy-state-token"
            else {"email": "appleuser@privaterelay.appleid.com", "sub": "apple-sub-123456"}
        ),
    ):
        with patch.dict(os.environ, env_settings):
            with patch.object(
                oauth, "_generate_apple_client_secret", return_value="apple_jwt_secret"
            ):
                with patch("httpx.Client.post") as mock_post:
                    mock_post.return_value.status_code = 200
                    mock_post.return_value.json.return_value = {
                        "access_token": "apple-access-token",
                        "id_token": dummy_apple_id_token,
                    }

                    resp = oauth.handle_oauth_callback(
                        "apple",
                        mock_request,
                        mock_response,
                        mock_db,
                        form_data=form_payload,
                    )
                    assert resp.status_code == 307 or resp.status_code == 302
                    assert resp.headers["location"] == "/catalog"
                    # User added with name Johnny Appleseed
                    assert mock_db.add.called
                    added_objects = [call[0][0] for call in mock_db.add.call_args_list]
                    user_created = next(
                        (obj for obj in added_objects if isinstance(obj, oauth.User)), None
                    )
                    assert user_created is not None
                    assert user_created.email == "appleuser@privaterelay.appleid.com"
                    assert user_created.display_name == "Johnny Appleseed"


def test_apple_callback_post_http_route():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.query.return_value.count.return_value = 1

    dummy_state = "client-post-state"
    state_cookie = jwt.encode(
        {"state": dummy_state, "next": "/home"}, oauth.SECRET_KEY, algorithm="HS256"
    )
    dummy_apple_id_token = jwt.encode(
        {"email": "postuser@apple.com", "sub": "apple-post-sub"},
        "dummy-secret",
        algorithm="HS256",
    )

    env_settings = {
        "APPLE_CLIENT_ID": "com.lake.service",
        "APPLE_TEAM_ID": "TEAM123456",
        "APPLE_KEY_ID": "KEY1234567",
        "APPLE_PRIVATE_KEY": "dummy_pk",
        "APPLE_ENABLED": "true",
    }

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    try:
        with patch.dict(os.environ, env_settings):
            with patch.object(
                oauth, "_generate_apple_client_secret", return_value="apple_jwt_secret"
            ):
                with patch("oauth.httpx.Client") as mock_httpx_cls:
                    mock_client_instance = MagicMock()
                    mock_client_instance.__enter__.return_value = mock_client_instance
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {
                        "access_token": "apple-access-token",
                        "id_token": dummy_apple_id_token,
                    }
                    mock_client_instance.post.return_value = mock_resp
                    mock_httpx_cls.return_value = mock_client_instance

                    backend_client.cookies.set("oauth_state", state_cookie)
                    resp = backend_client.post(
                        "/api/auth/oauth/apple/callback",
                        data={"code": "auth-code-apple", "state": dummy_state},
                        follow_redirects=False,
                    )
                    assert resp.status_code in (302, 307)
                    assert resp.headers["location"] == "/home"
                    assert "lake_token" in resp.headers.get("set-cookie", "")
    finally:
        backend_main.app.dependency_overrides.clear()
        backend_client.cookies.clear()
