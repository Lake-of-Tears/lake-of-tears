from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
ui_dir = str(repo_root / "ui")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import auth  # noqa: E402
from fastapi import FastAPI, Form  # noqa: E402
from starlette.templating import Jinja2Templates  # noqa: E402

# ── PyJWT Security & Token Validation Tests ───────────────────────────────────


def test_jwt_valid_token_roundtrip():
    payload = {"sub": "user@example.com", "role": "admin"}
    token = auth.create_token(payload, expires_delta=timedelta(minutes=15))
    decoded = auth.decode_token(token)

    assert decoded["sub"] == "user@example.com"
    assert decoded["role"] == "admin"
    assert "exp" in decoded
    assert "ss" in decoded


def test_jwt_expired_token_raises_expired_signature_error():
    payload = {"sub": "user@example.com", "role": "admin"}
    # Generate token that expired 10 seconds ago
    expired_token = auth.create_token(payload, expires_delta=timedelta(seconds=-10))

    with pytest.raises(jwt.ExpiredSignatureError):
        auth.decode_token(expired_token)


def test_jwt_tampered_payload_raises_invalid_token_error():
    payload = {"sub": "user@example.com", "role": "user"}
    token = auth.create_token(payload)

    # Tamper with the token payload part (split by dot)
    parts = token.split(".")
    # Replace payload with tampered base64 data
    tampered_token = (
        f"{parts[0]}.eyJzdWIiOiJhZG1pbkBleGFtcGxlLmNvbSIsInJvbGUiOiJzdXBlcmFkbWluIn0.{parts[2]}"
    )

    with pytest.raises(jwt.InvalidTokenError):
        auth.decode_token(tampered_token)


def test_jwt_wrong_secret_key_fails_validation():
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": "user@example.com", "exp": now + timedelta(minutes=5)},
        "completely-different-secret-key-32-chars!",
        algorithm="HS256",
    )

    with pytest.raises(jwt.InvalidTokenError):
        auth.decode_token(token)


def test_jwt_unsupported_or_none_algorithm_rejected():
    payload = {"sub": "user@example.com"}
    unsupported_token = jwt.encode(payload, key="", algorithm="none")

    with pytest.raises(jwt.InvalidTokenError):
        # auth.decode_token explicitly enforces algorithms=[ALGORITHM]
        auth.decode_token(unsupported_token)


# ── python-multipart Form Parsing Tests ───────────────────────────────────────


def test_fastapi_multipart_form_data_handling():
    app = FastAPI()

    @app.post("/test-form")
    async def handle_form(
        username: str = Form(...),
        comment: str = Form(""),
        count: int = Form(1),
    ):
        return {"username": username, "comment": comment, "count": count}

    client = TestClient(app)

    # Test standard application/x-www-form-urlencoded
    resp1 = client.post(
        "/test-form",
        data={
            "username": "alice",
            "comment": "test comment with special chars: &?=+",
            "count": "5",
        },
    )
    assert resp1.status_code == 200
    assert resp1.json() == {
        "username": "alice",
        "comment": "test comment with special chars: &?=+",
        "count": 5,
    }

    # Test multipart/form-data boundary parsing
    resp2 = client.post(
        "/test-form",
        data={"username": "bob", "comment": "multipart boundary check", "count": "2"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["username"] == "bob"


# ── Jinja2 & Starlette TemplateResponse Modern Signature Tests ───────────────


def test_starlette_template_response_signature_compatibility():
    templates = Jinja2Templates(directory="ui/templates")
    req = Request({"type": "http", "method": "GET", "path": "/login", "headers": []})

    resp = templates.TemplateResponse(
        request=req,
        name="login.html",
        context={"request": req},
    )
    assert resp.status_code == 200
    assert "Lake of Tears" in resp.body.decode("utf-8")
