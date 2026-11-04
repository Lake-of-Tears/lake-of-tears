from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
ui_dir = str(repo_root / "ui")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

# Load backend app
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_reset_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_reset_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

# Load UI app
ui_spec = importlib.util.spec_from_file_location("ui_main_reset_test", repo_root / "ui" / "main.py")
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_reset_test"] = ui_main
ui_spec.loader.exec_module(ui_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import Base, OAuthAccount, PasswordResetToken, User, UserSession  # noqa: E402


@pytest.fixture
def reset_db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()

    def override_get_db():
        try:
            yield session
        finally:
            pass

    backend_main.app.dependency_overrides[database.get_db] = override_get_db
    backend_main.app.dependency_overrides[auth.get_db] = override_get_db

    # Clear rate limit logs between tests
    auth._HELP_REQUESTS_LOG.clear()

    yield session

    session.close()
    Base.metadata.drop_all(bind=engine)
    backend_main.app.dependency_overrides.clear()


@pytest.fixture
def client(reset_db_session):
    return TestClient(backend_main.app)


@pytest.fixture
def ui_test_client():
    return TestClient(ui_main.app)


def test_forgot_password_for_password_user(client, reset_db_session):
    # Setup user with local password
    user = User(
        id=uuid.uuid4(),
        email="test_reset@example.com",
        display_name="Reset User",
        hashed_password=auth.hash_password("OldPassword123!"),
        role="user",
        is_active=True,
    )
    reset_db_session.add(user)
    reset_db_session.commit()

    with (
        patch.object(backend_main, "send_password_reset") as mock_reset_email,
        patch.object(backend_main, "send_sso_reminder") as mock_sso_email,
        patch.object(backend_main, "send_account_not_found") as mock_not_found_email,
    ):
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "test_reset@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "instructions to access your account" in data["message"]

        # Password reset email called
        mock_reset_email.assert_called_once()
        mock_sso_email.assert_not_called()
        mock_not_found_email.assert_not_called()

        call_args = mock_reset_email.call_args[1]
        assert call_args["to_email"] == "test_reset@example.com"
        assert "reset_token=" in call_args["reset_url"]

        # Verify token created in database
        token_rec = (
            reset_db_session.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id)
            .first()
        )
        assert token_rec is not None
        assert token_rec.used_at is None
        expires_at = token_rec.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        assert expires_at > datetime.now(UTC)


def test_forgot_password_for_sso_user(client, reset_db_session):
    # Setup user with SSO account only (no hashed_password)
    user = User(
        id=uuid.uuid4(),
        email="sso_user@example.com",
        display_name="SSO User",
        hashed_password=None,
        role="user",
        is_active=True,
    )
    reset_db_session.add(user)
    reset_db_session.flush()

    oauth_acc = OAuthAccount(
        user_id=user.id,
        provider="google",
        provider_user_id="google-uid-123",
    )
    reset_db_session.add(oauth_acc)
    reset_db_session.commit()

    with (
        patch.object(backend_main, "send_password_reset") as mock_reset_email,
        patch.object(backend_main, "send_sso_reminder") as mock_sso_email,
        patch.object(backend_main, "send_account_not_found") as mock_not_found_email,
    ):
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "sso_user@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

        mock_reset_email.assert_not_called()
        mock_sso_email.assert_called_once()
        mock_not_found_email.assert_not_called()

        call_args = mock_sso_email.call_args[1]
        assert call_args["to_email"] == "sso_user@example.com"
        assert call_args["providers"] == ["google"]

        # No reset token created in DB
        tokens = reset_db_session.query(PasswordResetToken).all()
        assert len(tokens) == 0


def test_forgot_password_for_non_existent_user(client, reset_db_session):
    with (
        patch.object(backend_main, "send_password_reset") as mock_reset_email,
        patch.object(backend_main, "send_sso_reminder") as mock_sso_email,
        patch.object(backend_main, "send_account_not_found") as mock_not_found_email,
    ):
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "nonexistent@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Privacy-safe identical message
        assert "instructions to access your account" in data["message"]

        mock_reset_email.assert_not_called()
        mock_sso_email.assert_not_called()
        mock_not_found_email.assert_called_once()
        assert mock_not_found_email.call_args[1]["to_email"] == "nonexistent@example.com"


def test_forgot_password_rate_limiting(client, reset_db_session):
    user = User(
        id=uuid.uuid4(),
        email="ratelimit@example.com",
        display_name="Rate Limit User",
        hashed_password=auth.hash_password("Password123!"),
        role="user",
        is_active=True,
    )
    reset_db_session.add(user)
    reset_db_session.commit()

    with patch("backend.main.send_password_reset"):
        # 1st request -> ok
        r1 = client.post("/api/auth/forgot-password", json={"email": "ratelimit@example.com"})
        assert r1.status_code == 200

        # 2nd request -> ok
        r2 = client.post("/api/auth/forgot-password", json={"email": "ratelimit@example.com"})
        assert r2.status_code == 200

        # 3rd request -> ok
        r3 = client.post("/api/auth/forgot-password", json={"email": "ratelimit@example.com"})
        assert r3.status_code == 200

        # 4th request -> 429 Too Many Requests
        r4 = client.post("/api/auth/forgot-password", json={"email": "ratelimit@example.com"})
        assert r4.status_code == 429
        assert "Too many requests" in r4.json()["detail"]


def test_verify_password_reset_token(client, reset_db_session):
    user = User(
        id=uuid.uuid4(),
        email="verify@example.com",
        display_name="Verify User",
        hashed_password=auth.hash_password("Password123!"),
        role="user",
        is_active=True,
    )
    reset_db_session.add(user)
    reset_db_session.commit()

    raw_token = auth.create_password_reset_token(reset_db_session, user)

    # Valid token
    res_valid = client.get(f"/api/auth/reset-password/verify?token={raw_token}")
    assert res_valid.status_code == 200
    assert res_valid.json() == {"valid": True, "email": "verify@example.com"}

    # Unknown token
    res_unknown = client.get("/api/auth/reset-password/verify?token=fake_token_123")
    assert res_unknown.status_code == 200
    assert res_unknown.json() == {"valid": False, "email": None}

    # Expired token
    expired_token = auth.create_password_reset_token(reset_db_session, user, valid_minutes=-10)
    res_expired = client.get(f"/api/auth/reset-password/verify?token={expired_token}")
    assert res_expired.status_code == 200
    assert res_expired.json() == {"valid": False, "email": None}


def test_reset_password_success_and_session_revocation(client, reset_db_session):
    user = User(
        id=uuid.uuid4(),
        email="reset_success@example.com",
        display_name="Reset Success",
        hashed_password=auth.hash_password("OldPassword123!"),
        role="user",
        is_active=True,
    )
    reset_db_session.add(user)
    reset_db_session.flush()

    # Create active sessions
    session1 = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        is_revoked=False,
    )
    session2 = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        is_revoked=False,
    )
    reset_db_session.add_all([session1, session2])
    reset_db_session.commit()

    raw_token = auth.create_password_reset_token(reset_db_session, user)

    # Perform reset
    resp = client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "NewSecretPassword456!",
            "confirm_password": "NewSecretPassword456!",
        },
    )
    assert resp.status_code == 200
    assert "Password reset successfully" in resp.json()["message"]

    # Verify user password updated
    reset_db_session.refresh(user)
    assert auth.verify_password("NewSecretPassword456!", user.hashed_password) is True
    assert auth.verify_password("OldPassword123!", user.hashed_password) is False

    # Verify sessions revoked
    reset_db_session.refresh(session1)
    reset_db_session.refresh(session2)
    assert session1.is_revoked is True
    assert session2.is_revoked is True

    # Verify token marked as used
    token_rec = (
        reset_db_session.query(PasswordResetToken)
        .filter(PasswordResetToken.user_id == user.id)
        .first()
    )
    assert token_rec.used_at is not None

    # Attempting to use the token again fails
    resp_reuse = client.post(
        "/api/auth/reset-password",
        json={
            "token": raw_token,
            "new_password": "AnotherPassword789!",
            "confirm_password": "AnotherPassword789!",
        },
    )
    assert resp_reuse.status_code == 400


def test_reset_password_validation_errors(client, reset_db_session):
    # Passwords do not match
    r1 = client.post(
        "/api/auth/reset-password",
        json={
            "token": "any_token",
            "new_password": "Password123!",
            "confirm_password": "PasswordMismatch!",
        },
    )
    assert r1.status_code == 422

    # Password too short
    r2 = client.post(
        "/api/auth/reset-password",
        json={
            "token": "any_token",
            "new_password": "short",
            "confirm_password": "short",
        },
    )
    assert r2.status_code == 422


def test_ui_login_page_renders_help_elements(ui_test_client):
    resp = ui_test_client.get("/login")
    assert resp.status_code == 200
    content = resp.text

    # Link under password
    assert "Need help signing in?" in content
    assert "openHelpModal()" in content

    # Modal elements
    assert 'id="help-modal"' in content
    assert 'id="help-email"' in content
    assert 'id="btn-help-submit"' in content

    # Reset password form
    assert 'id="form-reset"' in content
    assert 'id="reset-new-password"' in content
    assert 'id="reset-confirm-password"' in content
    assert 'id="btn-reset-submit"' in content
