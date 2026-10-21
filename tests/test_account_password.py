from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

# Add backend and ui paths to sys.path
repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
ui_dir = str(repo_root / "ui")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import auth  # noqa: E402
import database  # noqa: E402
from models import User  # noqa: E402

# Load backend app
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_account_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_account_test"] = backend_main
backend_spec.loader.exec_module(backend_main)
backend_client = TestClient(backend_main.app)

# Load ui app
ui_spec = importlib.util.spec_from_file_location(
    "ui_main_account_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_account_test"] = ui_main
ui_spec.loader.exec_module(ui_main)
ui_client = TestClient(ui_main.app)


def _make_user(
    password: str | None = "OldPassword123!",
    is_active: bool = True,
    email: str = "test@example.com",
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name="Test User",
        role="user",
        is_active=is_active,
        hashed_password=auth.hash_password(password) if password else None,
        created_at=datetime.now(UTC),
    )
    return user


def _make_token(user: User) -> str:
    return auth.create_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name or "",
        }
    )


# ── Backend API Tests ────────────────────────────────────────────────────────


def test_change_password_unauthenticated_fails():
    backend_main.app.dependency_overrides.clear()
    resp = backend_client.post(
        "/api/auth/change-password",
        json={"current_password": "OldPassword123!", "new_password": "NewPassword123!"},
    )
    assert resp.status_code == 401


def test_change_password_success_and_login_verification():
    user = _make_user("InitialPass123!")
    token = _make_token(user)

    mock_db = MagicMock()

    def _query_side_effect(model):
        q = MagicMock()
        if model == User:
            q.filter.return_value.first.return_value = user
        else:
            q.filter.return_value.first.return_value = None
        return q

    mock_db.query.side_effect = _query_side_effect

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        # 1. Change password successfully
        resp = backend_client.post(
            "/api/auth/change-password",
            json={"current_password": "InitialPass123!", "new_password": "UpdatedPass456!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert "token" in data
        assert mock_db.commit.called

        # User's hash must now verify with the new password
        assert auth.verify_password("UpdatedPass456!", user.hashed_password)
        assert not auth.verify_password("InitialPass123!", user.hashed_password)

        # 2. Login with old password fails
        login_fail = backend_client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "InitialPass123!"},
        )
        assert login_fail.status_code == 401

        # 3. Login with new password succeeds
        login_success = backend_client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "UpdatedPass456!"},
        )
        assert login_success.status_code == 200
        assert login_success.json().get("ok") is True
    finally:
        backend_main.app.dependency_overrides.clear()


def test_change_password_incorrect_current_password():
    user = _make_user("CorrectPassword1!")
    token = _make_token(user)
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = backend_client.post(
            "/api/auth/change-password",
            json={"current_password": "WrongPassword!", "new_password": "NewSecretPassword!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Incorrect current password"
        assert auth.verify_password("CorrectPassword1!", user.hashed_password)
    finally:
        backend_main.app.dependency_overrides.clear()


def test_change_password_same_as_current_rejected():
    user = _make_user("SamePassword123!")
    token = _make_token(user)
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = backend_client.post(
            "/api/auth/change-password",
            json={"current_password": "SamePassword123!", "new_password": "SamePassword123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert "different from current password" in resp.text
    finally:
        backend_main.app.dependency_overrides.clear()


def test_change_password_too_short_rejected():
    user = _make_user("CurrentPassword123!")
    token = _make_token(user)
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = backend_client.post(
            "/api/auth/change-password",
            json={"current_password": "CurrentPassword123!", "new_password": "short"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert "at least 8 characters" in resp.text
    finally:
        backend_main.app.dependency_overrides.clear()


def test_change_password_oauth_only_user_rejected():
    user = _make_user(password=None)  # OAuth-only user (no hashed_password)
    assert not user.has_password
    token = _make_token(user)
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = backend_client.post(
            "/api/auth/change-password",
            json={"current_password": "AnyPassword123!", "new_password": "NewValidPassword123!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert "SSO/OAuth" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


# ── UI Route & Template Tests ────────────────────────────────────────────────


def test_ui_settings_account_renders_change_password_card_for_standard_user():
    user = _make_user("MyPassword123!")
    token = _make_token(user)

    mock_profile = {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": True,
        "created_at": datetime.now(UTC).isoformat(),
        "has_password": True,
    }

    with patch.object(ui_main, "_backend_get", return_value=mock_profile):
        resp = ui_client.get(
            "/settings/account",
            cookies={"lake_token": token},
        )
        assert resp.status_code == 200
        assert "Change Password" in resp.text
        assert 'name="current_password"' in resp.text
        assert 'name="new_password"' in resp.text
        assert 'name="confirm_password"' in resp.text


def test_ui_settings_account_renders_sso_notice_for_oauth_user():
    user = _make_user(password=None)
    token = _make_token(user)

    mock_profile = {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": True,
        "created_at": datetime.now(UTC).isoformat(),
        "has_password": False,
    }

    with patch.object(ui_main, "_backend_get", return_value=mock_profile):
        resp = ui_client.get(
            "/settings/account",
            cookies={"lake_token": token},
        )
        assert resp.status_code == 200
        assert "Single Sign-On (SSO) / OAuth" in resp.text
        assert 'name="current_password"' not in resp.text


def test_ui_settings_account_renders_success_and_error_banners():
    user = _make_user("MyPassword123!")
    token = _make_token(user)

    # 1. Success banner
    resp_saved = ui_client.get(
        "/settings/account?password_saved=1",
        cookies={"lake_token": token},
    )
    assert resp_saved.status_code == 200
    assert "Password updated successfully." in resp_saved.text
    assert "alert-password-saved" in resp_saved.text

    # 2. Error banner
    resp_err = ui_client.get(
        "/settings/account?password_error=Incorrect+current+password",
        cookies={"lake_token": token},
    )
    assert resp_err.status_code == 200
    assert "Incorrect current password" in resp_err.text
    assert "alert-password-error" in resp_err.text


def test_ui_post_password_mismatched_confirm_redirects_with_error():
    user = _make_user("MyPassword123!")
    token = _make_token(user)

    resp = ui_client.post(
        "/settings/account/password",
        data={
            "current_password": "MyPassword123!",
            "new_password": "NewSecret123!",
            "confirm_password": "DifferentSecret!",
        },
        cookies={"lake_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "password_error=" in resp.headers["location"]
    assert "New+passwords+do+not+match" in resp.headers["location"]


def test_ui_post_password_too_short_redirects_with_error():
    user = _make_user("MyPassword123!")
    token = _make_token(user)

    resp = ui_client.post(
        "/settings/account/password",
        data={
            "current_password": "MyPassword123!",
            "new_password": "short",
            "confirm_password": "short",
        },
        cookies={"lake_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "password_error=" in resp.headers["location"]
    assert "at+least+8+characters" in resp.headers["location"]


def test_ui_post_password_backend_error_redirects_with_error():
    user = _make_user("MyPassword123!")
    token = _make_token(user)

    with patch.object(
        ui_main, "_backend_post_with_error", return_value=(None, "Incorrect current password")
    ):
        resp = ui_client.post(
            "/settings/account/password",
            data={
                "current_password": "WrongPassword123!",
                "new_password": "ValidNewPassword123!",
                "confirm_password": "ValidNewPassword123!",
            },
            cookies={"lake_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "password_error=Incorrect+current+password" in resp.headers["location"]


def test_ui_post_password_success_redirects_and_updates_cookie():
    user = _make_user("OldPass123!")
    token = _make_token(user)
    new_fake_token = _make_token(user)

    with patch.object(
        ui_main,
        "_backend_post_with_error",
        return_value=({"ok": True, "token": new_fake_token}, None),
    ):
        resp = ui_client.post(
            "/settings/account/password",
            data={
                "current_password": "OldPass123!",
                "new_password": "NewValidPassword123!",
                "confirm_password": "NewValidPassword123!",
            },
            cookies={"lake_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings/account?password_saved=1"
        assert "lake_token=" in resp.headers.get("set-cookie", "")


def test_get_current_user_supports_role_and_email_sub():
    user = _make_user("OldPass123!", email="superadmin@example.com")
    user.role = "superadmin"
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = user

    import jwt

    # 1. Test with sub="superadmin"
    token_role = jwt.encode({"sub": "superadmin"}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    u1 = auth.get_current_user(lake_token=token_role, db=mock_db)
    assert u1.email == "superadmin@example.com"

    # 2. Test with sub="superadmin@example.com"
    token_email = jwt.encode(
        {"sub": "superadmin@example.com"}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    u2 = auth.get_current_user(lake_token=token_email, db=mock_db)
    assert u2.email == "superadmin@example.com"
