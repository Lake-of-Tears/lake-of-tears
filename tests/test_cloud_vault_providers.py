from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    "backend_main_cloud_secrets_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_cloud_secrets_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    SecretScope,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
)
from secrets_manager.aws_sm import AWSSecretsManagerProvider  # noqa: E402
from secrets_manager.azure_kv import AzureKeyVaultProvider  # noqa: E402
from secrets_manager.config import (  # noqa: E402
    get_vault_provider_config,
    mask_vault_provider_config,
    set_vault_provider_config,
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

    yield session


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


# ── Azure Key Vault Unit Tests ────────────────────────────────────────────────


def test_azure_keyvault_provider_operations():
    scope = SecretScope(
        id=uuid.uuid4(),
        name="azure-scope",
        backend_type="azure_keyvault",
        provider_config={"vault_url": "https://mytestvault.vault.azure.net/"},
    )

    prov = AzureKeyVaultProvider(
        {
            "vault_url": "https://mytestvault.vault.azure.net/",
            "tenant_id": "test-tenant",
            "client_id": "test-client",
            "client_secret": "test-secret",
            "auth_mode": "service_principal",
        }
    )

    mock_client = MagicMock()
    mock_secret = MagicMock()
    mock_secret.value = "my_azure_secret_val"
    mock_client.get_secret.return_value = mock_secret

    mock_prop1 = MagicMock()
    mock_prop1.name = "key-one"
    mock_prop1.enabled = True
    mock_prop2 = MagicMock()
    mock_prop2.name = "key-two"
    mock_prop2.enabled = True
    mock_client.list_properties_of_secrets.return_value = [mock_prop1, mock_prop2]

    with patch.object(prov, "_get_client", return_value=mock_client):
        # 1. get_secret
        val = prov.get_secret(scope, "key-one")
        assert val == "my_azure_secret_val"
        mock_client.get_secret.assert_called_with(name="key-one")

        # 2. put_secret
        prov.put_secret(scope, "key-three", "secret_value_3")
        mock_client.set_secret.assert_called_with(name="key-three", value="secret_value_3")

        # 3. delete_secret
        res = prov.delete_secret(scope, "key-one")
        assert res is True
        mock_client.begin_delete_secret.assert_called_with(name="key-one")

        # 4. list_keys
        keys = prov.list_keys(scope)
        assert keys == ["key-one", "key-two"]

        # 5. test_connection
        ok, msg = prov.test_connection()
        assert ok is True
        assert "mytestvault" in msg


# ── AWS Secrets Manager Unit Tests ───────────────────────────────────────────


def test_aws_secrets_manager_provider_operations():
    scope = SecretScope(
        id=uuid.uuid4(),
        name="aws-scope",
        backend_type="aws_secrets_manager",
        provider_config={"region_name": "us-east-1", "prefix": "lake-of-tears/prod/"},
    )

    prov = AWSSecretsManagerProvider(
        {
            "region_name": "us-east-1",
            "aws_access_key_id": "AKIAEXAMPLE",
            "aws_secret_access_key": "SECRETEXAMPLE",
            "prefix": "lake-of-tears/prod/",
        }
    )

    mock_client = MagicMock()
    mock_client.get_secret_value.return_value = {"SecretString": "my_aws_secret_val"}

    paginator_mock = MagicMock()
    paginator_mock.paginate.return_value = [
        {
            "SecretList": [
                {"Name": "lake-of-tears/prod/db_pass"},
                {"Name": "lake-of-tears/prod/api_key"},
                {"Name": "other/service/secret"},  # outside prefix
            ]
        }
    ]
    mock_client.get_paginator.return_value = paginator_mock

    with patch.object(prov, "_get_client", return_value=mock_client):
        # 1. get_secret
        val = prov.get_secret(scope, "db_pass")
        assert val == "my_aws_secret_val"
        mock_client.get_secret_value.assert_called_with(SecretId="lake-of-tears/prod/db_pass")

        # 2. put_secret
        prov.put_secret(scope, "new_key", "val_123")
        mock_client.put_secret_value.assert_called_with(
            SecretId="lake-of-tears/prod/new_key", SecretString="val_123"
        )

        # 3. delete_secret
        res = prov.delete_secret(scope, "db_pass")
        assert res is True
        mock_client.delete_secret.assert_called_with(
            SecretId="lake-of-tears/prod/db_pass", ForceDeleteWithoutRecovery=True
        )

        # 4. list_keys
        keys = prov.list_keys(scope)
        assert keys == ["api_key", "db_pass"]

        # 5. test_connection
        with patch("boto3.client") as mock_boto:
            mock_test_client = MagicMock()
            mock_boto.return_value = mock_test_client
            ok, msg = prov.test_connection()
            assert ok is True
            assert "us-east-1" in msg


# ── Configuration Encryption & Masking Tests ─────────────────────────────────


def test_vault_config_persistence_and_masking(db_session):
    super_user, _ = create_user_and_token(db_session, "superadmin@example.com", role="superadmin")

    raw_azure = {
        "vault_url": "https://companyvault.vault.azure.net/",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "client_id": "11111111-1111-1111-1111-111111111111",
        "client_secret": "SuperSecretAzurePassword123!",
        "auth_mode": "service_principal",
    }

    # Save encrypted
    set_vault_provider_config(db_session, "azure_keyvault", raw_azure, user_id=super_user.id)

    # Read back decrypted
    saved = get_vault_provider_config(db_session, "azure_keyvault")
    assert saved == raw_azure

    # Masked output
    masked = mask_vault_provider_config("azure_keyvault", saved)
    assert masked["vault_url"] == "https://companyvault.vault.azure.net/"
    assert masked["client_secret"].startswith("••••••••")
    assert "SuperSecretAzurePassword123!" not in masked["client_secret"]


# ── Superadmin Admin API Tests ───────────────────────────────────────────────


def test_admin_vault_providers_api(client, db_session):
    super_user, super_token = create_user_and_token(
        db_session, "boss@example.com", role="superadmin"
    )
    regular_user, regular_token = create_user_and_token(
        db_session, "regular@example.com", role="user"
    )

    headers_super = {"Authorization": f"Bearer {super_token}"}
    headers_regular = {"Authorization": f"Bearer {regular_token}"}

    # 1. Non-superadmin forbidden (403)
    res = client.get("/api/admin/vault-providers", headers=headers_regular)
    assert res.status_code == 403

    # 2. Superadmin lists providers
    res = client.get("/api/admin/vault-providers", headers=headers_super)
    assert res.status_code == 200
    providers = res.json()
    provider_names = [p["provider"] for p in providers]
    assert "internal" in provider_names
    assert "azure_keyvault" in provider_names
    assert "aws_secrets_manager" in provider_names

    # 3. Superadmin configures Azure Key Vault
    az_payload = {
        "config": {
            "vault_url": "https://my-prod-vault.vault.azure.net/",
            "tenant_id": "tid-123",
            "client_id": "cid-456",
            "client_secret": "mySecretKeyXYZ99",
            "auth_mode": "service_principal",
        }
    }
    res = client.put(
        "/api/admin/vault-providers/azure_keyvault", headers=headers_super, json=az_payload
    )
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "azure_keyvault"
    assert data["is_configured"] is True
    assert data["config"]["vault_url"] == "https://my-prod-vault.vault.azure.net/"
    assert data["config"]["client_secret"].startswith("••••••••")

    # 4. Updating without changing secret preserves original secret
    update_url_only = {
        "config": {
            "vault_url": "https://my-prod-vault-updated.vault.azure.net/",
            "tenant_id": "tid-123",
            "client_id": "cid-456",
            "client_secret": data["config"]["client_secret"],  # sends back masked string
            "auth_mode": "service_principal",
        }
    }
    res = client.put(
        "/api/admin/vault-providers/azure_keyvault", headers=headers_super, json=update_url_only
    )
    assert res.status_code == 200
    # Verify underlying DB still holds original unmasked secret
    stored = get_vault_provider_config(db_session, "azure_keyvault")
    assert stored["client_secret"] == "mySecretKeyXYZ99"
    assert stored["vault_url"] == "https://my-prod-vault-updated.vault.azure.net/"

    # 5. Test connection endpoint
    with patch(
        "secrets_manager.azure_kv.AzureKeyVaultProvider.test_connection",
        return_value=(True, "Ping ok"),
    ):
        res = client.post(
            "/api/admin/vault-providers/azure_keyvault/test", headers=headers_super, json={}
        )
        assert res.status_code == 200
        assert res.json()["success"] is True
        assert res.json()["message"] == "Ping ok"


# ── Cloud-Backed Scope End-to-End API Integration ────────────────────────────


def test_cloud_backed_secret_scope_integration(client, db_session):
    admin_user, admin_token = create_user_and_token(db_session, "cloudadmin@example.com")
    ws = create_workspace(db_session, "Cloud Project", "cloud-proj", admin_user)
    headers = {"Authorization": f"Bearer {admin_token}", "X-Workspace-Id": str(ws.id)}

    # Save AWS configuration in system settings
    set_vault_provider_config(
        db_session,
        "aws_secrets_manager",
        {"region_name": "us-west-2", "prefix": "lot/cloud-proj/"},
        user_id=admin_user.id,
    )

    # Create AWS-backed secret scope
    res = client.post(
        "/api/v1/secrets/scopes",
        headers=headers,
        json={"name": "aws-cloud-scope", "backend_type": "aws_secrets_manager"},
    )
    assert res.status_code == 201
    assert res.json()["backend_type"] == "aws_secrets_manager"

    mock_aws_client = MagicMock()
    mock_aws_client.get_secret_value.return_value = {"SecretString": "production-api-token-999"}
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [{"SecretList": [{"Name": "lot/cloud-proj/api_token"}]}]
    mock_aws_client.get_paginator.return_value = mock_paginator

    with patch(
        "secrets_manager.aws_sm.AWSSecretsManagerProvider._get_client", return_value=mock_aws_client
    ):
        # 1. Put secret into AWS-backed scope
        res = client.post(
            "/api/v1/secrets/scopes/aws-cloud-scope/keys",
            headers=headers,
            json={"key": "api_token", "value": "production-api-token-999"},
        )
        assert res.status_code == 201
        assert res.json()["key"] == "api_token"

        # 2. List keys in AWS-backed scope
        res = client.get("/api/v1/secrets/scopes/aws-cloud-scope/keys", headers=headers)
        assert res.status_code == 200
        keys = res.json()
        assert len(keys) == 1
        assert keys[0]["key"] == "api_token"

        # 3. Retrieve secret value from AWS-backed scope
        res = client.get("/api/v1/secrets/scopes/aws-cloud-scope/keys/api_token", headers=headers)
        assert res.status_code == 200
        assert res.json()["value"] == "production-api-token-999"

        # 4. Delete secret from AWS-backed scope
        res = client.delete(
            "/api/v1/secrets/scopes/aws-cloud-scope/keys/api_token", headers=headers
        )
        assert res.status_code == 200
        assert res.json()["status"] == "deleted"
