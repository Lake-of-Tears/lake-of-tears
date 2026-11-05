from __future__ import annotations

import importlib.util
import io
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    "backend_main_runtime_secrets_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_runtime_secrets_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

import auth  # noqa: E402
import database  # noqa: E402
import lake_of_tears.secrets as lot_secrets  # noqa: E402
import lot.secrets as lot_alias_secrets  # noqa: E402
from models import (  # noqa: E402
    Base,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
)
from pipeline_service import get_task_log  # noqa: E402
from secrets_manager.redaction import (  # noqa: E402
    RedactingStream,
    SecretRedactionFilter,
    clear_registered_secrets,
    redact_secrets,
    register_secret_for_redaction,
)
from secrets_manager.service import SecretManagerService  # noqa: E402
from secrets_manager.templating import (  # noqa: E402
    has_secret_templates,
    resolve_secret_templates,
)


@pytest.fixture
def db_session():
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

    clear_registered_secrets()

    yield session

    clear_registered_secrets()


@pytest.fixture
def client(db_session):
    return TestClient(backend_main.app)


def create_user_and_token(db, email: str, role: str = "user") -> tuple[User, str]:
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0],
        hashed_password=auth.hash_password("Password123!"),
        role=role,
        is_active=True,
    )
    db.add(user)
    sess_id = uuid.uuid4()
    db.add(
        UserSession(
            id=sess_id,
            user_id=user.id,
            created_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            is_revoked=False,
        )
    )
    db.commit()
    db.refresh(user)
    token = auth.create_token(
        {"sub": str(user.id), "email": user.email, "role": user.role},
        session_id=str(sess_id),
    )
    return user, token


def create_workspace(db, name: str, slug: str, owner: User) -> Workspace:
    ws = Workspace(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        created_by=owner.id,
        is_system=False,
        status="active",
    )
    db.add(ws)
    db.flush()
    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=owner.id,
        role="admin",
    )
    db.add(member)
    db.commit()
    db.refresh(ws)
    return ws


# ── Python SDK Client Tests ───────────────────────────────────────────────────


def test_python_sdk_secrets_client(db_session, monkeypatch):
    user, token = create_user_and_token(db_session, "sdk_user@example.com")
    ws = create_workspace(db_session, "Data Science", "ds-workspace", user)

    svc = SecretManagerService(db_session)
    scope = svc.create_scope(ws.id, "mlflow-creds", user=user)
    svc.put_secret_value(scope, "api_key", "secret-mlflow-key-998877", user)
    svc.put_secret_value(scope, "tracking_token", "secret-token-112233", user)

    # Configure environment for SDK client
    monkeypatch.setenv("WORKSPACE_ID", str(ws.id))
    monkeypatch.setenv("LAKE_TOKEN", token)

    # Mock database.SessionLocal in SDK to use our test session
    with patch("database.SessionLocal", return_value=db_session):
        # 1. get secret via lake_of_tears.secrets
        val = lot_secrets.get("mlflow-creds", "api_key")
        assert val == "secret-mlflow-key-998877"

        # 2. get secret via lot.secrets alias
        val2 = lot_alias_secrets.get("mlflow-creds", "tracking_token")
        assert val2 == "secret-token-112233"

        # 3. list keys
        keys = lot_secrets.list("mlflow-creds")
        assert "api_key" in keys
        assert "tracking_token" in keys

        # 4. list scopes
        scopes = lot_secrets.list_scopes()
        assert "mlflow-creds" in scopes

        # 5. Verify the retrieved secret was registered with redaction filter automatically
        test_log = (
            "Connected to MLflow with key secret-mlflow-key-998877 and token secret-token-112233"
        )
        sanitized = redact_secrets(test_log)
        assert "secret-mlflow-key-998877" not in sanitized
        assert "secret-token-112233" not in sanitized
        assert sanitized == "Connected to MLflow with key [REDACTED] and token [REDACTED]"


# ── Template Placeholder Interpolation Tests ──────────────────────────────────


def test_template_interpolation_variants(db_session):
    user, _ = create_user_and_token(db_session, "template_user@example.com")
    ws = create_workspace(db_session, "ETL Pipeline", "etl-pipeline", user)

    svc = SecretManagerService(db_session)
    scope = svc.create_scope(ws.id, "database-creds", user=user)
    svc.put_secret_value(scope, "pg_password", "myP@ssw0rd!123", user)
    svc.put_secret_value(scope, "pg_user", "postgres_admin", user)

    # 1. has_secret_templates checks
    assert (
        has_secret_templates(
            "postgresql://user:{{secrets/database-creds/pg_password}}@host:5432/db"
        )
        is True
    )
    assert (
        has_secret_templates(
            "postgresql://user:{{secrets.database-creds.pg_password}}@host:5432/db"
        )
        is True
    )
    assert (
        has_secret_templates("postgresql://user:${secrets:database-creds:pg_password}@host:5432/db")
        is True
    )
    assert has_secret_templates("postgresql://user:regular_password@host:5432/db") is False
    assert has_secret_templates({"url": "{{secrets/database-creds/pg_password}}"}) is True

    # 2. String interpolation with multiple formats
    raw_conn = (
        "postgresql://{{secrets/database-creds/pg_user}}:"
        "{{ secrets.database-creds.pg_password }}@db.prod.internal:5432/lakehouse"
    )
    resolved = resolve_secret_templates(raw_conn, ws.id, db_session, user=user)
    assert resolved == "postgresql://postgres_admin:myP@ssw0rd!123@db.prod.internal:5432/lakehouse"

    # Shell style ${secrets:...}
    raw_shell = "export DB_KEY=${secrets:database-creds:pg_password}"
    assert (
        resolve_secret_templates(raw_shell, ws.id, db_session, user=user)
        == "export DB_KEY=myP@ssw0rd!123"
    )

    # 3. Recursive dictionary and list resolution
    raw_config = {
        "connections": [
            {
                "name": "primary",
                "auth": {
                    "user": "{{secrets/database-creds/pg_user}}",
                    "pass": "{{secrets/database-creds/pg_password}}",
                },
            }
        ],
        "timeout": 30,
    }
    resolved_config = resolve_secret_templates(raw_config, ws.id, db_session, user=user)
    assert resolved_config["connections"][0]["auth"]["user"] == "postgres_admin"
    assert resolved_config["connections"][0]["auth"]["pass"] == "myP@ssw0rd!123"
    assert resolved_config["timeout"] == 30

    # 4. Unknown scope or key raises KeyError
    with pytest.raises(KeyError):
        resolve_secret_templates("{{secrets/unknown-scope/key}}", ws.id, db_session, user=user)

    # 5. Permission denied raises HTTPException(403)
    outsider, _ = create_user_and_token(db_session, "outsider@example.com")
    with pytest.raises(Exception):
        resolve_secret_templates(
            "{{secrets/database-creds/pg_password}}", ws.id, db_session, user=outsider
        )


# ── Real-Time Output Redaction and Masking Tests ──────────────────────────────


def test_real_time_log_redaction_and_streaming():
    clear_registered_secrets()

    secret1 = "super-confidential-api-token-7766"
    secret2 = "super-confidential-api-token"  # Substring of secret1
    register_secret_for_redaction(secret1)
    register_secret_for_redaction(secret2)

    # 1. Length-descending substitution prevents partial replacement
    sample_text = f"Headers: Authorization: Bearer {secret1} and secondary token {secret2}"
    redacted = redact_secrets(sample_text)
    assert secret1 not in redacted
    assert secret2 not in redacted
    assert redacted == "Headers: Authorization: Bearer [REDACTED] and secondary token [REDACTED]"

    # 2. LoT raw envelope cipher token redaction
    envelope_token = "v1:r38yKk1-L1e4n8x9:8c149d68a9b3:2a9b4c8d1e"
    log_line = f"Database setting saved: value_str={envelope_token}"
    assert redact_secrets(log_line) == "Database setting saved: value_str=[REDACTED_SECRET_TOKEN]"

    # 3. RedactingStream interception
    mem_stream = io.StringIO()
    redacting_stream = RedactingStream(mem_stream)
    redacting_stream.write(f"Connecting with {secret1} ... connected.\n")
    output = mem_stream.getvalue()
    assert secret1 not in output
    assert output == "Connecting with [REDACTED] ... connected.\n"

    # 4. SecretRedactionFilter in standard Python logging
    filter_instance = SecretRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Task failed using secret credentials: %s",
        args=(secret1,),
        exc_info=None,
    )
    filter_instance.filter(record)
    assert secret1 not in str(record.args)
    assert "[REDACTED]" in str(record.args)


@pytest.mark.anyio
async def test_pipeline_task_log_redaction():
    secret_token = "secret-airflow-run-pass-5544"
    register_secret_for_redaction(secret_token)

    fake_raw_log = f"""
    [2026-11-04 22:00:00] INFO - Executing task with token: {secret_token}
    [2026-11-04 22:00:01] INFO - Query executed successfully.
    """

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"content": fake_raw_log}
    mock_resp.text = fake_raw_log

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_resp

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("pipeline_service.get_dag_details", return_value={}),
    ):
        result = await get_task_log(
            workspace_slug="analytics",
            dag_id="daily_sync",
            run_id="run_123",
            task_id="sync_task",
        )
        content = result["content"]
        assert secret_token not in content
        assert "[REDACTED]" in content
