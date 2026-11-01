from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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
    "backend_main_session_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_session_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

# Explicitly load ui_main
ui_spec = importlib.util.spec_from_file_location(
    "ui_main_session_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_session_test"] = ui_main
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
    # Clean tables
    session.query(UserSession).delete()
    session.query(User).delete()
    session.query(SystemSetting).delete()
    # Seed default system settings
    session.add(SystemSetting(key="session_inactivity_timeout_minutes", value=60))
    session.add(SystemSetting(key="session_max_hours", value=24))
    session.commit()
    session.close()


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    backend_main.app.dependency_overrides[database.get_db] = override_get_db
    backend_main.app.dependency_overrides.pop(auth.get_current_user, None)
    c = TestClient(backend_main.app)
    yield c
    backend_main.app.dependency_overrides.clear()


def _create_user(
    db, email: str = "test@example.com", role: str = "user", password: str = "Secret123!"
):
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        hashed_password=auth.hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_login_creates_session_in_db_with_jti(client, db_session):
    user = _create_user(db_session, "user1@example.com")
    resp = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "Secret123!"},
        headers={"User-Agent": "PyTest-Client/1.0"},
    )
    assert resp.status_code == 200
    token = client.cookies.get(auth.COOKIE_NAME)
    assert token is not None

    payload = auth.decode_token(token)
    assert "jti" in payload
    session_id = uuid.UUID(payload["jti"])

    sess = db_session.query(UserSession).filter(UserSession.id == session_id).first()
    assert sess is not None
    assert sess.user_id == user.id
    assert sess.is_revoked is False
    assert "PyTest-Client" in (sess.user_agent or "")


def test_missing_jti_rejected_401(db_session):
    user = _create_user(db_session, "legacy@example.com")
    now = datetime.now(UTC)
    legacy_token = jwt.encode(
        {"sub": str(user.id), "exp": now + timedelta(hours=1)},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(Exception) as exc:
        auth.get_current_user(lake_token=legacy_token, db=db_session)
    assert "401" in str(exc.value)


def test_revoked_session_rejected_even_with_valid_jwt(db_session):
    user = _create_user(db_session, "revoked@example.com")
    sess = auth.create_user_session(
        db_session, user, timedelta(hours=1), ip_address="127.0.0.1", user_agent="TestAgent"
    )
    token = auth.create_token({"sub": str(user.id)}, session_id=str(sess.id))

    # Before revocation: valid
    u = auth.get_current_user(lake_token=token, db=db_session)
    assert u.id == user.id

    # Revoke session in DB
    sess.is_revoked = True
    sess.revoked_at = datetime.now(UTC)
    db_session.commit()

    # After revocation: rejected with 401
    with pytest.raises(Exception) as exc:
        auth.get_current_user(lake_token=token, db=db_session)
    assert "Session has been revoked" in str(exc.value)


def test_explicit_logout_revokes_session_in_db(client, db_session):
    user = _create_user(db_session, "logout@example.com")
    resp = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "Secret123!"},
    )
    assert resp.status_code == 200
    token = client.cookies.get(auth.COOKIE_NAME)
    payload = auth.decode_token(token)
    session_id = uuid.UUID(payload["jti"])

    sess = db_session.query(UserSession).filter(UserSession.id == session_id).first()
    assert sess.is_revoked is False

    # Logout
    logout_resp = client.post("/api/auth/logout", cookies={auth.COOKIE_NAME: token})
    assert logout_resp.status_code == 200

    db_session.refresh(sess)
    assert sess.is_revoked is True
    assert sess.revoked_at is not None


def test_session_refresh_slides_session_window(client, db_session):
    user = _create_user(db_session, "refresh@example.com")
    sess = auth.create_user_session(
        db_session, user, timedelta(minutes=15), ip_address="127.0.0.1", user_agent="TestAgent"
    )
    token = auth.create_token(
        {"sub": str(user.id)}, expires_delta=timedelta(minutes=15), session_id=str(sess.id)
    )

    old_exp = sess.expires_at
    resp = client.post("/api/auth/refresh", cookies={auth.COOKIE_NAME: token})
    assert resp.status_code == 200

    db_session.refresh(sess)
    assert sess.expires_at > old_exp
    assert sess.is_revoked is False


def test_get_user_sessions_endpoint(client, db_session):
    user = _create_user(db_session, "sessions@example.com")
    sess1 = auth.create_user_session(
        db_session, user, timedelta(hours=1), ip_address="10.0.0.1", user_agent="Firefox/Mac"
    )
    sess2 = auth.create_user_session(
        db_session, user, timedelta(hours=1), ip_address="10.0.0.2", user_agent="Chrome/Phone"
    )
    token = auth.create_token({"sub": str(user.id)}, session_id=str(sess1.id))

    resp = client.get("/api/auth/sessions", cookies={auth.COOKIE_NAME: token})
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 2

    # Verify is_current flag
    curr = next(s for s in sessions if s["id"] == str(sess1.id))
    other = next(s for s in sessions if s["id"] == str(sess2.id))
    assert curr["is_current"] is True
    assert other["is_current"] is False


def test_revoke_other_sessions(client, db_session):
    user = _create_user(db_session, "revoke_others@example.com")
    sess1 = auth.create_user_session(db_session, user, timedelta(hours=1), ip_address="1.1.1.1")
    sess2 = auth.create_user_session(db_session, user, timedelta(hours=1), ip_address="2.2.2.2")
    sess3 = auth.create_user_session(db_session, user, timedelta(hours=1), ip_address="3.3.3.3")
    token = auth.create_token({"sub": str(user.id)}, session_id=str(sess1.id))

    resp = client.post("/api/auth/sessions/revoke-others", cookies={auth.COOKIE_NAME: token})
    assert resp.status_code == 200
    assert resp.json()["revoked_count"] == 2

    db_session.refresh(sess1)
    db_session.refresh(sess2)
    db_session.refresh(sess3)
    assert sess1.is_revoked is False
    assert sess2.is_revoked is True
    assert sess3.is_revoked is True


def test_admin_list_and_revoke_sessions(client, db_session):
    admin = _create_user(db_session, "admin@example.com", role="superadmin")
    user = _create_user(db_session, "victim@example.com", role="user")

    admin_sess = auth.create_user_session(db_session, admin, timedelta(hours=1))
    user_sess = auth.create_user_session(db_session, user, timedelta(hours=1))

    admin_token = auth.create_token({"sub": str(admin.id)}, session_id=str(admin_sess.id))
    user_token = auth.create_token({"sub": str(user.id)}, session_id=str(user_sess.id))

    # User cannot access admin sessions
    forbidden = client.get("/api/admin/sessions", cookies={auth.COOKIE_NAME: user_token})
    assert forbidden.status_code == 403

    # Admin can list all sessions
    resp = client.get("/api/admin/sessions", cookies={auth.COOKIE_NAME: admin_token})
    assert resp.status_code == 200
    data = resp.json()
    assert any(s["id"] == str(user_sess.id) for s in data)

    # Admin revokes victim's session
    del_resp = client.delete(
        f"/api/admin/sessions/{user_sess.id}", cookies={auth.COOKIE_NAME: admin_token}
    )
    assert del_resp.status_code == 200
    db_session.refresh(user_sess)
    assert user_sess.is_revoked is True


def test_prune_expired_sessions(db_session):
    user = _create_user(db_session, "prune@example.com")
    now = datetime.now(UTC)

    # 1. Active session (keep)
    s_active = auth.create_user_session(db_session, user, timedelta(hours=1))
    id_active = s_active.id

    # 2. Recently revoked session (< 30 days) (keep for audit)
    s_recent_revoked = auth.create_user_session(db_session, user, timedelta(hours=1))
    s_recent_revoked.is_revoked = True
    s_recent_revoked.revoked_at = now - timedelta(days=5)
    id_recent = s_recent_revoked.id

    # 3. Old revoked session (> 30 days) (prune)
    s_old_revoked = auth.create_user_session(db_session, user, timedelta(hours=1))
    s_old_revoked.is_revoked = True
    s_old_revoked.revoked_at = now - timedelta(days=35)
    id_old_revoked = s_old_revoked.id

    # 4. Old expired session (> 30 days) (prune)
    s_old_expired = auth.create_user_session(db_session, user, timedelta(hours=1))
    s_old_expired.expires_at = now - timedelta(days=35)
    id_old_expired = s_old_expired.id

    db_session.commit()
    db_session.expunge_all()

    pruned = auth.prune_expired_sessions(db_session, retention_days=30)
    assert pruned == 2

    remaining_ids = [s.id for s in db_session.query(UserSession).all()]
    assert id_active in remaining_ids
    assert id_recent in remaining_ids
    assert id_old_revoked not in remaining_ids
    assert id_old_expired not in remaining_ids


def test_ui_settings_account_renders_sessions():
    client = TestClient(ui_main.app)
    user_id = str(uuid.uuid4())
    token = jwt.encode(
        {"sub": user_id, "email": "alice@example.com", "role": "user", "jti": str(uuid.uuid4())},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    fake_sessions = [
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "ip_address": "192.168.1.50",
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)",
            "created_at": "2026-11-01 12:00:00",
            "last_active_at": "2026-11-01 12:30:00",
            "expires_at": "2026-11-01 13:30:00",
            "is_revoked": False,
            "is_current": True,
        }
    ]

    with patch.object(ui_main, "_backend_get") as mock_get:
        mock_get.side_effect = (
            lambda path, tok: fake_sessions
            if path == "/api/auth/sessions"
            else {"display_name": "Alice"}
        )
        resp = client.get("/settings/account", cookies={"lake_token": token})
        assert resp.status_code == 200
        assert "Active Sessions" in resp.text
        assert "Current Device" in resp.text
        assert "192.168.1.50" in resp.text
