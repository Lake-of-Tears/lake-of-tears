from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
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

# Explicitly load backend_main to prevent collisions with ui/main.py
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_inactivity_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_inactivity_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

# Explicitly load ui_main
ui_spec = importlib.util.spec_from_file_location(
    "ui_main_inactivity_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_inactivity_test"] = ui_main
ui_spec.loader.exec_module(ui_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import Base, SystemSetting, User, UserSession  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def setup_db():
    session = TestingSessionLocal()
    database.SessionLocal = TestingSessionLocal
    backend_main.get_db = lambda: session
    try:
        session.query(UserSession).delete()
        session.query(SystemSetting).delete()
        session.query(User).delete()
        session.commit()
        yield session
    finally:
        session.close()


def _create_user(session, email="inactivity@example.com", role="user") -> User:
    u = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=auth.hash_password("Password123!"),
        role=role,
        is_active=True,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_ui_logout_preserves_inactivity_reason_and_clears_cookies():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "user@example.com", "role": "user"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    with patch.object(ui_main, "_backend_post") as mock_post:
        resp = client.get(
            "/logout?reason=inactivity", cookies={"lake_token": token, "lake_workspace": "ws-1"}
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?reason=inactivity"
        mock_post.assert_called_once_with("/api/auth/logout", token, {})

        # Verify Set-Cookie headers clear both lake_token and lake_workspace
        set_cookies = (
            resp.headers.get_list("set-cookie")
            if hasattr(resp.headers, "get_list")
            else [resp.headers.get("set-cookie", "")]
        )
        cookie_text = " ".join(set_cookies)
        assert "lake_token=" in cookie_text
        assert "lake_workspace=" in cookie_text


def test_ui_login_with_reason_does_not_redirect_even_with_valid_token():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "user@example.com", "role": "user"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    # 1. Normal visit with token redirects to home
    resp_home = client.get("/login", cookies={"lake_token": token})
    assert resp_home.status_code == 302
    assert resp_home.headers["location"] == "/"

    # 2. Visit with ?reason=inactivity stays on login page and displays notice
    resp_inactive = client.get("/login?reason=inactivity", cookies={"lake_token": token})
    assert resp_inactive.status_code == 200
    assert "You were signed out due to inactivity." in resp_inactive.text

    # Verify cookie clearance in response
    set_cookies = (
        resp_inactive.headers.get_list("set-cookie")
        if hasattr(resp_inactive.headers, "get_list")
        else [resp_inactive.headers.get("set-cookie", "")]
    )
    cookie_text = " ".join(set_cookies)
    assert "lake_token=" in cookie_text


def test_ui_api_auth_logout_endpoint():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "user@example.com", "role": "user"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    with patch.object(ui_main, "_backend_post") as mock_post:
        resp = client.post("/api/auth/logout", cookies={"lake_token": token})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        mock_post.assert_called_once_with("/api/auth/logout", token, {})

        set_cookies = (
            resp.headers.get_list("set-cookie")
            if hasattr(resp.headers, "get_list")
            else [resp.headers.get("set-cookie", "")]
        )
        cookie_text = " ".join(set_cookies)
        assert "lake_token=" in cookie_text
        assert "lake_workspace=" in cookie_text


def test_ui_api_auth_refresh_proxy_success():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "user@example.com", "role": "user"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}
    mock_resp.cookies = {"lake_token": "new-slid-token"}

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        resp = client.post("/api/auth/refresh", cookies={"lake_token": token})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        set_cookies = (
            resp.headers.get_list("set-cookie")
            if hasattr(resp.headers, "get_list")
            else [resp.headers.get("set-cookie", "")]
        )
        cookie_text = " ".join(set_cookies)
        assert "lake_token=new-slid-token" in cookie_text


def test_ui_api_auth_refresh_proxy_unauthenticated():
    client = TestClient(ui_main.app, follow_redirects=False)
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_backend_auth_refresh_accepts_bearer_header(setup_db):
    user = _create_user(setup_db)
    token = auth.create_token({"sub": str(user.id), "email": user.email, "role": user.role})

    client = TestClient(backend_main.app)
    # Call refresh without lake_token cookie, but with Authorization: Bearer <token>
    resp = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    set_cookies = (
        resp.headers.get_list("set-cookie")
        if hasattr(resp.headers, "get_list")
        else [resp.headers.get("set-cookie", "")]
    )
    cookie_text = " ".join(set_cookies)
    assert "lake_token=" in cookie_text


def test_base_template_inactivity_bindings():
    base_html_path = repo_root / "ui" / "templates" / "base.html"
    content = base_html_path.read_text(encoding="utf-8")

    # Inactivity modal elements
    assert 'id="inactivity-modal"' in content
    assert 'id="inactivity-stay-btn"' in content
    assert 'id="inactivity-signout-btn"' in content
    assert 'onclick="stayLoggedIn()"' in content
    assert 'onclick="signOutNow()"' in content

    # Global window function bindings
    assert "window.stayLoggedIn = stayLoggedIn;" in content
    assert "window.signOutNow = signOutNow;" in content

    # Explicit event listeners on elements
    assert "stayBtn.addEventListener('click', stayLoggedIn);" in content
    assert "signoutBtn.addEventListener('click', signOutNow);" in content

    # Dynamic scaling for warning window
    assert "Math.min(5 * 60 * 1000, Math.max(15 * 1000, Math.floor(_TIMEOUT_MS * 0.25)))" in content

    # Guard against passive pointer resetting warning while modal is active
    assert "if (!_warningActive) {" in content


def test_update_system_settings_request_validation():
    import pydantic
    from schemas import UpdateSystemSettingsRequest

    # Allowed: 0 (disabled), 1–1440 minutes
    for val in [0, 1, 2, 5, 10, 15, 60, 480, 720, 1440]:
        req = UpdateSystemSettingsRequest(session_inactivity_timeout_minutes=val)
        assert req.session_inactivity_timeout_minutes == val

    # Disallowed: negative or > 1440 minutes
    for val in [-1, -60, 1441, 2000]:
        with pytest.raises(pydantic.ValidationError):
            UpdateSystemSettingsRequest(session_inactivity_timeout_minutes=val)


def test_backend_admin_settings_saves_short_inactivity_timeout(setup_db):
    import datetime

    user = _create_user(setup_db, role="superadmin")
    sess = UserSession(
        user_id=user.id,
        ip_address="127.0.0.1",
        user_agent="test",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    setup_db.add(sess)
    setup_db.commit()
    token = auth.create_token(
        {"sub": str(user.id), "email": user.email, "role": user.role},
        session_id=str(sess.id),
    )

    client = TestClient(backend_main.app)
    # Test setting 5 minutes (previously rejected by 15-480 constraint)
    resp = client.patch(
        "/api/admin/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"session_inactivity_timeout_minutes": 5, "session_max_hours": 12},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_inactivity_timeout_minutes"] == 5
    assert data["session_max_hours"] == 12

    # Verify persisted in database
    row = (
        setup_db.query(SystemSetting)
        .filter(SystemSetting.key == "session_inactivity_timeout_minutes")
        .first()
    )
    assert row is not None
    assert row.value == 5


def test_ui_session_settings_saves_and_invalidates_cache(monkeypatch):
    import ui_main_inactivity_test as ui_mod

    # Setup initial cache
    ui_mod._SESSION_CONFIG_CACHE = {
        "session_inactivity_timeout_minutes": 60,
        "session_max_hours": 24,
    }
    ui_mod._SESSION_CONFIG_FETCHED_AT = 1000.0

    mock_patch_with_error = MagicMock(
        return_value=({"session_inactivity_timeout_minutes": 5}, None)
    )
    monkeypatch.setattr(ui_mod, "_backend_patch_with_error", mock_patch_with_error)

    client = TestClient(ui_mod.app, follow_redirects=False)

    # Superadmin session token using UI secret key
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "admin@example.com", "role": "superadmin"},
        ui_mod.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    resp = client.post(
        "/settings/admin/session-settings",
        data={"session_inactivity_timeout_minutes": "5", "session_max_hours": "12"},
        cookies={"lake_token": token},
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings/admin?saved=session_saved#platform"
    mock_patch_with_error.assert_called_once_with(
        "/api/admin/settings",
        token,
        {"session_inactivity_timeout_minutes": 5, "session_max_hours": 12},
    )
    # Verify cache was invalidated
    assert ui_mod._SESSION_CONFIG_CACHE is None
    assert ui_mod._SESSION_CONFIG_FETCHED_AT == 0.0
