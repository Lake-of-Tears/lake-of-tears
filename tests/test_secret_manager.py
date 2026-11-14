from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
    "backend_main_secrets_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_secrets_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    SecretAcl,
    SecretKey,
    SecretScope,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
)
from secrets_manager.internal import InternalSecretProvider  # noqa: E402
from secrets_manager.service import SecretManagerService  # noqa: E402


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

    yield session


@pytest.fixture
def client(db_session):
    return TestClient(backend_main.app)


def create_user_and_token(db, email: str, role: str = "user") -> tuple[User, str]:
    from datetime import timedelta

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


# ── Internal Secret Provider Tests ────────────────────────────────────────────


def test_internal_secret_provider_crud_and_rotation(db_session):
    user, _ = create_user_and_token(db_session, "owner@example.com")
    ws = create_workspace(db_session, "Analytics", "analytics", user)

    scope = SecretScope(
        workspace_id=ws.id,
        name="finance-keys",
        backend_type="internal",
        created_by=user.id,
    )
    db_session.add(scope)
    db_session.commit()

    provider = InternalSecretProvider(db_session)

    # 1. Test test_connection
    ok, msg = provider.test_connection()
    assert ok is True
    assert "operational" in msg.lower()

    # 2. Put secret
    provider.put_secret(scope, "stripe_api_key", "sk_live_123456789", user_id=user.id)

    # 3. Get secret
    val = provider.get_secret(scope, "stripe_api_key")
    assert val == "sk_live_123456789"

    # 4. Verify raw DB record is encrypted
    rec = (
        db_session.query(SecretKey)
        .filter(SecretKey.scope_id == scope.id, SecretKey.key == "stripe_api_key")
        .first()
    )
    assert rec is not None
    assert rec.encrypted_value != "sk_live_123456789"
    assert rec.encrypted_value.startswith("v1:")
    assert rec.version == 1

    # 5. Rotate secret
    provider.put_secret(scope, "stripe_api_key", "sk_live_999999999", user_id=user.id)
    assert provider.get_secret(scope, "stripe_api_key") == "sk_live_999999999"
    db_session.refresh(rec)
    assert rec.version == 2

    # 6. List keys (returns names only)
    keys = provider.list_keys(scope)
    assert keys == ["stripe_api_key"]

    # 7. Delete secret
    deleted = provider.delete_secret(scope, "stripe_api_key")
    assert deleted is True
    assert provider.list_keys(scope) == []

    # 8. Get nonexistent raises KeyError
    with pytest.raises(KeyError):
        provider.get_secret(scope, "stripe_api_key")


# ── SecretManagerService ACL and Permission Tests ─────────────────────────────


def test_secret_manager_service_permissions_hierarchy(db_session):
    admin_user, _ = create_user_and_token(db_session, "admin@example.com")
    writer_user, _ = create_user_and_token(db_session, "writer@example.com")
    reader_user, _ = create_user_and_token(db_session, "reader@example.com")
    outsider_user, _ = create_user_and_token(db_session, "outsider@example.com")
    superadmin_user, _ = create_user_and_token(db_session, "super@example.com", role="superadmin")

    ws = create_workspace(db_session, "DataOps", "dataops", admin_user)

    # Add members
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=writer_user.id, role="writer"))
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=reader_user.id, role="reader"))
    db_session.commit()

    svc = SecretManagerService(db_session)
    scope = svc.create_scope(ws.id, "prod-credentials", user=admin_user)

    # Creator / Admin has MANAGE
    assert svc.resolve_user_permission(scope, admin_user, "admin") == "MANAGE"
    # Superadmin has MANAGE
    assert svc.resolve_user_permission(scope, superadmin_user, None) == "MANAGE"

    # Outsider has None
    assert svc.resolve_user_permission(scope, outsider_user, None) is None

    # Grant WRITE to writer_user
    svc.set_acl(scope, "user", str(writer_user.id), "WRITE", admin_user)
    assert svc.resolve_user_permission(scope, writer_user, "writer") == "WRITE"

    # Grant READ to all readers via role ACL
    svc.set_acl(scope, "role", "reader", "READ", admin_user)
    assert svc.resolve_user_permission(scope, reader_user, "reader") == "READ"

    # Verify require_permission enforcement
    # Writer can satisfy WRITE and READ, but not MANAGE
    assert svc.require_permission(scope, writer_user, "writer", "READ") == "WRITE"
    assert svc.require_permission(scope, writer_user, "writer", "WRITE") == "WRITE"
    with pytest.raises(Exception):
        svc.require_permission(scope, writer_user, "writer", "MANAGE")

    # Reader can satisfy READ, but not WRITE or MANAGE
    assert svc.require_permission(scope, reader_user, "reader", "READ") == "READ"
    with pytest.raises(Exception):
        svc.require_permission(scope, reader_user, "reader", "WRITE")


# ── REST API Endpoints End-to-End Tests ───────────────────────────────────────


def test_secrets_api_full_flow(client, db_session):
    admin_user, admin_token = create_user_and_token(db_session, "wsadmin@example.com")
    reader_user, reader_token = create_user_and_token(db_session, "reader_user@example.com")
    ws = create_workspace(db_session, "Engineering", "engineering", admin_user)

    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=reader_user.id, role="reader"))
    db_session.commit()

    headers_admin = {
        "Authorization": f"Bearer {admin_token}",
        "X-Workspace-Id": str(ws.id),
    }
    headers_reader = {
        "Authorization": f"Bearer {reader_token}",
        "X-Workspace-Id": str(ws.id),
    }

    # 1. Non-admin member cannot create scope (403)
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes",
        headers=headers_reader,
        json={"name": "forbidden-scope"},
    )
    assert res.status_code == 403

    # 2. Workspace Admin creates scope
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes",
        headers=headers_admin,
        json={"name": "aws-tokens", "backend_type": "internal"},
    )
    assert res.status_code == 201
    scope_data = res.json()
    assert scope_data["name"] == "aws-tokens"
    assert scope_data["backend_type"] == "internal"
    assert scope_data["user_permission"] == "MANAGE"
    scope_id = scope_data["id"]

    # 3. Duplicate scope creation rejected (409)
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes",
        headers=headers_admin,
        json={"name": "aws-tokens"},
    )
    assert res.status_code == 409

    # 4. Put secret key into scope
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys",
        headers=headers_admin,
        json={"key": "aws_secret_access_key", "value": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"},
    )
    assert res.status_code == 201
    key_summary = res.json()
    assert key_summary["key"] == "aws_secret_access_key"
    assert key_summary["version"] == 1
    # Plaintext value MUST NOT be in the summary
    assert "value" not in key_summary

    # 5. List keys in scope (returns metadata, no plaintext)
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys",
        headers=headers_admin,
    )
    assert res.status_code == 200
    keys_list = res.json()
    assert len(keys_list) == 1
    assert keys_list[0]["key"] == "aws_secret_access_key"
    assert "value" not in keys_list[0]

    # 6. Retrieve secret key plaintext value
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_admin,
    )
    assert res.status_code == 200
    secret_val_data = res.json()
    assert secret_val_data["key"] == "aws_secret_access_key"
    assert secret_val_data["value"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert secret_val_data["version"] == 1

    # 7. Reader without ACL cannot access secret (403)
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_reader,
    )
    assert res.status_code == 403

    # 8. Grant READ ACL to reader user
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/acls",
        headers=headers_admin,
        json={"principal_type": "user", "principal_id": str(reader_user.id), "permission": "READ"},
    )
    assert res.status_code == 200
    acl_data = res.json()
    acl_id = acl_data["id"]
    assert acl_data["permission"] == "READ"

    # 9. Reader can now fetch the secret value
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_reader,
    )
    assert res.status_code == 200
    assert res.json()["value"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    # 10. Reader cannot write or rotate secret (403)
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys",
        headers=headers_reader,
        json={"key": "new_key", "value": "val"},
    )
    assert res.status_code == 403

    # 11. Rotate secret as admin -> version increments to 2
    res = client.post(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys",
        headers=headers_admin,
        json={"key": "aws_secret_access_key", "value": "ROTATED_VALUE_ABC_123"},
    )
    assert res.status_code == 201
    assert res.json()["version"] == 2

    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_admin,
    )
    assert res.json()["value"] == "ROTATED_VALUE_ABC_123"
    assert res.json()["version"] == 2

    # 12. Delete ACL
    res = client.delete(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/acls/{acl_id}",
        headers=headers_admin,
    )
    assert res.status_code == 200

    # 13. Reader is blocked again (403)
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_reader,
    )
    assert res.status_code == 403

    # 14. Delete secret key
    res = client.delete(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_admin,
    )
    assert res.status_code == 200

    # Key is now 404
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}/keys/aws_secret_access_key",
        headers=headers_admin,
    )
    assert res.status_code == 404

    # 15. Delete scope
    res = client.delete(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}",
        headers=headers_admin,
    )
    assert res.status_code == 200

    # Scope is now 404
    res = client.get(
        f"/api/v1/workspaces/{ws.id}/secrets/scopes/{scope_id}",
        headers=headers_admin,
    )
    assert res.status_code == 404


def test_secrets_workspace_isolation_and_scoping(client, db_session):
    user_a, token_a = create_user_and_token(db_session, "user_a@example.com")
    user_b, token_b = create_user_and_token(db_session, "user_b@example.com")

    ws_a = create_workspace(db_session, "Workspace A", "ws-a", user_a)
    ws_b = create_workspace(db_session, "Workspace B", "ws-b", user_b)

    headers_a = {"Authorization": f"Bearer {token_a}", "X-Workspace-Id": str(ws_a.id)}
    headers_b = {"Authorization": f"Bearer {token_b}", "X-Workspace-Id": str(ws_b.id)}

    # User A creates scope in Workspace A
    res = client.post(
        "/api/v1/secrets/scopes",
        headers=headers_a,
        json={"name": "secret-scope-a"},
    )
    assert res.status_code == 201
    scope_a_id = res.json()["id"]

    # User A adds a secret
    res = client.post(
        f"/api/v1/secrets/scopes/{scope_a_id}/keys",
        headers=headers_a,
        json={"key": "db_password", "value": "supersecretA"},
    )
    assert res.status_code == 201

    # User B listing scopes in Workspace B should see empty list
    res = client.get("/api/v1/secrets/scopes", headers=headers_b)
    assert res.status_code == 200
    assert len(res.json()) == 0

    # User B attempting to access Scope A in Workspace B or Workspace A gets 403 or 404
    res = client.get(f"/api/v1/secrets/scopes/{scope_a_id}", headers=headers_b)
    assert res.status_code == 404

    # User B trying to specify ws_a in path without membership gets 403
    res = client.get(
        f"/api/v1/workspaces/{ws_a.id}/secrets/scopes/{scope_a_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res.status_code == 403


def test_secrets_name_based_lookup_and_superadmin_bypass(client, db_session):
    admin_user, admin_token = create_user_and_token(db_session, "admin2@example.com")
    super_user, super_token = create_user_and_token(
        db_session, "super2@example.com", role="superadmin"
    )

    ws = create_workspace(db_session, "Analytics Pro", "analytics-pro", admin_user)

    headers_admin = {"Authorization": f"Bearer {admin_token}", "X-Workspace-Id": str(ws.id)}
    headers_super = {"Authorization": f"Bearer {super_token}", "X-Workspace-Id": str(ws.id)}

    # Admin creates scope "snowflake-creds"
    res = client.post(
        "/api/v1/secrets/scopes",
        headers=headers_admin,
        json={"name": "snowflake-creds"},
    )
    assert res.status_code == 201

    # Admin puts secret using scope name (instead of UUID)
    res = client.post(
        "/api/v1/secrets/scopes/snowflake-creds/keys",
        headers=headers_admin,
        json={"key": "account", "value": "xy12345.us-east-1"},
    )
    assert res.status_code == 201

    # Superadmin (not an explicit member of the workspace) can access and read the secret
    res = client.get(
        "/api/v1/secrets/scopes/snowflake-creds/keys/account",
        headers=headers_super,
    )
    assert res.status_code == 200
    assert res.json()["value"] == "xy12345.us-east-1"

    # Superadmin can write/rotate keys
    res = client.post(
        "/api/v1/secrets/scopes/snowflake-creds/keys",
        headers=headers_super,
        json={"key": "role", "value": "SYSADMIN"},
    )
    assert res.status_code == 201

    # Verify key list contains both
    res = client.get("/api/v1/secrets/scopes/snowflake-creds/keys", headers=headers_admin)
    assert res.status_code == 200
    keys = [k["key"] for k in res.json()]
    assert "account" in keys
    assert "role" in keys


def test_secrets_validation_and_cascade(client, db_session):
    admin_user, admin_token = create_user_and_token(db_session, "validator@example.com")
    ws = create_workspace(db_session, "Validation WS", "validation-ws", admin_user)
    headers = {"Authorization": f"Bearer {admin_token}", "X-Workspace-Id": str(ws.id)}

    # Invalid scope name (spaces or special characters)
    res = client.post(
        "/api/v1/secrets/scopes", headers=headers, json={"name": "invalid name with spaces!"}
    )
    assert res.status_code == 422 or res.status_code == 400

    # Valid creation
    res = client.post("/api/v1/secrets/scopes", headers=headers, json={"name": "valid-scope_1.0"})
    assert res.status_code == 201
    scope_id = res.json()["id"]

    # Invalid key name
    res = client.post(
        f"/api/v1/secrets/scopes/{scope_id}/keys",
        headers=headers,
        json={"key": "bad key!@#", "value": "foo"},
    )
    assert res.status_code == 422 or res.status_code == 400

    # Put valid key
    res = client.post(
        f"/api/v1/secrets/scopes/{scope_id}/keys",
        headers=headers,
        json={"key": "valid_key", "value": "bar"},
    )
    assert res.status_code == 201

    # Verify DB records exist
    assert (
        db_session.query(SecretKey).filter(SecretKey.scope_id == uuid.UUID(scope_id)).count() == 1
    )
    assert (
        db_session.query(SecretAcl).filter(SecretAcl.scope_id == uuid.UUID(scope_id)).count() == 1
    )

    # Delete scope
    res = client.delete(f"/api/v1/secrets/scopes/{scope_id}", headers=headers)
    assert res.status_code == 200

    # Verify cascading delete wiped keys and acls
    assert (
        db_session.query(SecretKey).filter(SecretKey.scope_id == uuid.UUID(scope_id)).count() == 0
    )
    assert (
        db_session.query(SecretAcl).filter(SecretAcl.scope_id == uuid.UUID(scope_id)).count() == 0
    )


def test_pipeline_task_token_read_rotate_and_audit(client, db_session):
    admin_user, admin_token = create_user_and_token(db_session, "admin-task@example.com")
    create_workspace(db_session, "TMD 291", "default", admin_user)
    other_ws = create_workspace(db_session, "Other WS", "other-ws", admin_user)
    admin_headers = {"Authorization": f"Bearer {admin_token}", "X-Workspace-Slug": "default"}

    # 1. Create scope tmd-291 and seed a key
    res = client.post(
        "/api/v1/secrets/scopes",
        headers=admin_headers,
        json={"name": "tmd-291", "backend_type": "internal"},
    )
    assert res.status_code == 201

    res = client.put(
        "/api/v1/secrets/scopes/tmd-291/keys/GUSTO_SESSION_STORAGE_STATE_B64",
        headers=admin_headers,
        json={"value": "initial-session-v1"},
    )
    assert res.status_code == 200
    assert res.json()["version"] == 1

    # 2. Mint a task token for a read-only bundle (e.g. tmd-291-sales-and-product-mix-pipeline)
    sales_token = auth.create_task_token(
        workspace_slug="default",
        bundle_name="tmd_291_sales_and_product_mix_pipeline",
        pipeline_name="tmd_291_sales_pipeline",
        task_name="fetch_revel_sales",
    )
    sales_headers = {"Authorization": f"Bearer {sales_token}"}

    # Before granting bundle ACL, even reading is 403
    res = client.get(
        "/api/v1/secrets/scopes/tmd-291/keys/GUSTO_SESSION_STORAGE_STATE_B64",
        headers=sales_headers,
    )
    assert res.status_code == 403

    # 3. Grant READ to bundle:* and WRITE to bundle:tmd-291-payroll-pipeline
    res = client.post(
        "/api/v1/secrets/scopes/tmd-291/acls",
        headers=admin_headers,
        json={"principal_type": "bundle", "principal_id": "*", "permission": "READ"},
    )
    assert res.status_code == 200

    res = client.post(
        "/api/v1/secrets/scopes/tmd-291/acls",
        headers=admin_headers,
        json={
            "principal_type": "bundle",
            "principal_id": "tmd-291-payroll-pipeline",
            "permission": "WRITE",
        },
    )
    assert res.status_code == 200

    # 4. Read-only bundle can now read keys and values, but gets 403 on PUT/POST/DELETE
    res = client.get(
        "/api/v1/secrets/scopes/tmd-291/keys/GUSTO_SESSION_STORAGE_STATE_B64",
        headers=sales_headers,
    )
    assert res.status_code == 200
    assert res.json()["value"] == "initial-session-v1"
    assert res.json()["version"] == 1

    res = client.put(
        "/api/v1/secrets/scopes/tmd-291/keys/GUSTO_SESSION_STORAGE_STATE_B64",
        headers=sales_headers,
        json={"value": "unauthorized-overwrite"},
    )
    assert res.status_code == 403

    # Cross-workspace access with task token is rejected with 403
    res = client.get(
        "/api/v1/secrets/scopes/tmd-291/keys",
        headers={**sales_headers, "X-Workspace-Slug": other_ws.slug},
    )
    assert res.status_code == 403

    # 5. Payroll bundle (with WRITE permission, hyphen/underscore normalized) can rotate the secret
    payroll_token = auth.create_task_token(
        workspace_slug="default",
        bundle_name="tmd_291_payroll_pipeline",
        pipeline_name="tmd_291_payroll_pipeline",
        task_name="fetch_payroll_runs",
    )
    payroll_headers = {"Authorization": f"Bearer {payroll_token}"}

    res = client.put(
        "/api/v1/secrets/scopes/tmd-291/keys/GUSTO_SESSION_STORAGE_STATE_B64",
        headers=payroll_headers,
        json={"value": "rotated-session-v2"},
    )
    assert res.status_code == 200
    assert res.json()["version"] == 2

    # Subsequent GET returns v2 immediately
    res = client.get(
        "/api/v1/secrets/scopes/tmd-291/keys/GUSTO_SESSION_STORAGE_STATE_B64",
        headers=payroll_headers,
    )
    assert res.status_code == 200
    assert res.json()["value"] == "rotated-session-v2"
    assert res.json()["version"] == 2

    # 6. Verify audit log records both writes (user v1 + pipeline v2)
    res = client.get("/api/v1/secrets/scopes/tmd-291/audit", headers=admin_headers)
    assert res.status_code == 200
    audit_entries = res.json()
    assert len(audit_entries) >= 2
    latest = audit_entries[0]
    assert latest["key"] == "GUSTO_SESSION_STORAGE_STATE_B64"
    assert latest["action"] == "PUT_SECRET"
    assert latest["version"] == 2
    assert latest["actor_type"] == "pipeline"
    assert (
        latest["actor_id"] == "tmd_291_payroll_pipeline/tmd_291_payroll_pipeline/fetch_payroll_runs"
    )
    assert latest["bundle_name"] == "tmd_291_payroll_pipeline"
    assert latest["task_name"] == "fetch_payroll_runs"

    # 7. Verify generate_bundle_dag_code embeds _mint_task_token and LOT_TASK_TOKEN
    from bundles.deployer import generate_bundle_dag_code
    from bundles.schemas import PipelineResource, PipelineTask

    pipe = PipelineResource(
        name="tmd_291_payroll_pipeline",
        schedule=None,
        tasks=[
            PipelineTask(
                name="fetch_payroll_runs",
                type="python_script",
                entrypoint="tasks/fetch_payroll_runs.py",
                timeout_seconds=1800,
            )
        ],
    )
    dag_id, dag_code = generate_bundle_dag_code(
        workspace_slug="default",
        bundle_name="tmd-291-payroll-pipeline",
        target_name="dev",
        pipeline=pipe,
    )
    assert dag_id == "ws_default_tmd_291_payroll_pipeline_tmd_291_payroll_pipeline"
    assert "LOT_TASK_TOKEN" in dag_code
    assert "_mint_task_token" in dag_code
    assert '"pipeline_name": "tmd_291_payroll_pipeline"' in dag_code

    # 8. Verify Alembic migration chain has a single head (0026)
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_cfg = Config(str(repo_root / "backend" / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(repo_root / "backend" / "alembic"))
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    assert script_dir.get_heads() == ["0026"]
