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
from secrets_manager.gcp_sm import GCPSecretManagerProvider  # noqa: E402
from secrets_manager.hashicorp_vault import HashiCorpVaultProvider  # noqa: E402


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
    assert "hashicorp_vault" in provider_names
    assert "gcp_secret_manager" in provider_names

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
    admin_user, admin_token = create_user_and_token(
        db_session, "cloudadmin@example.com", role="superadmin"
    )
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

    # 5. Audit all scopes endpoint
    res = client.get("/api/admin/vault-providers/scopes", headers=headers)
    assert res.status_code == 200
    scopes = res.json()
    assert isinstance(scopes, list)
    assert any(s["name"] == "aws-cloud-scope" for s in scopes)


# ── HashiCorp Vault Unit Tests ────────────────────────────────────────────────


def test_hashicorp_vault_provider_kv2_operations():
    scope = SecretScope(
        id=uuid.uuid4(),
        name="vault-scope",
        backend_type="hashicorp_vault",
        provider_config={"mount_point": "secret", "kv_version": 2, "prefix": "lot-prefix"},
    )

    prov = HashiCorpVaultProvider(
        {
            "vault_url": "https://vault.corp.internal:8200",
            "token": "s.test-token-12345",
            "mount_point": "secret",
            "kv_version": 2,
            "prefix": "lot-prefix",
        }
    )

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.sys.is_sealed.return_value = False

    # KV v2 responses
    mock_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"value": "vault_secret_val_42", "db_pass": "vault_secret_val_42"}}
    }
    mock_client.secrets.kv.v2.list_secrets.return_value = {
        "data": {"keys": ["db_pass", "api_key/"]}
    }

    with patch.object(prov, "_get_client", return_value=mock_client):
        # 1. get_secret
        val = prov.get_secret(scope, "db_pass")
        assert val == "vault_secret_val_42"
        mock_client.secrets.kv.v2.read_secret_version.assert_called_with(
            path="lot-prefix/vault-scope/db_pass", mount_point="secret"
        )

        # 2. put_secret
        prov.put_secret(scope, "new_key", "secret_val_99")
        mock_client.secrets.kv.v2.create_or_update_secret.assert_called_with(
            path="lot-prefix/vault-scope/new_key",
            secret={"value": "secret_val_99", "new_key": "secret_val_99"},
            mount_point="secret",
        )

        # 3. delete_secret
        res = prov.delete_secret(scope, "db_pass")
        assert res is True
        mock_client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_with(
            path="lot-prefix/vault-scope/db_pass", mount_point="secret"
        )

        # 4. list_keys
        keys = prov.list_keys(scope)
        assert "api_key" in keys
        assert "db_pass" in keys

        # 5. test_connection
        with patch.object(prov, "_create_client", return_value=mock_client):
            ok, msg = prov.test_connection()
            assert ok is True
            assert "Successfully connected" in msg
            assert "vault.corp.internal" in msg


def test_hashicorp_vault_provider_kv1_operations():
    scope = SecretScope(
        id=uuid.uuid4(),
        name="vault-v1-scope",
        backend_type="hashicorp_vault",
        provider_config={"mount_point": "kv", "kv_version": 1},
    )

    prov = HashiCorpVaultProvider(
        {
            "vault_url": "https://vault.internal:8200",
            "token": "s.token-v1",
            "mount_point": "kv",
            "kv_version": 1,
        }
    )

    mock_client = MagicMock()
    mock_client.is_authenticated.return_value = True
    mock_client.secrets.kv.v1.read_secret.return_value = {"data": {"value": "kv1_val"}}
    mock_client.secrets.kv.v1.list_secrets.return_value = {"data": {"keys": ["token_a"]}}

    with patch.object(prov, "_get_client", return_value=mock_client):
        # 1. get_secret
        val = prov.get_secret(scope, "token_a")
        assert val == "kv1_val"
        mock_client.secrets.kv.v1.read_secret.assert_called_with(
            path="vault-v1-scope/token_a", mount_point="kv"
        )

        # 2. put_secret
        prov.put_secret(scope, "token_b", "val_b")
        mock_client.secrets.kv.v1.create_or_update_secret.assert_called_with(
            path="vault-v1-scope/token_b",
            secret={"value": "val_b", "token_b": "val_b"},
            mount_point="kv",
        )

        # 3. delete_secret
        res = prov.delete_secret(scope, "token_a")
        assert res is True
        mock_client.secrets.kv.v1.delete_secret.assert_called_with(
            path="vault-v1-scope/token_a", mount_point="kv"
        )

        # 4. list_keys
        keys = prov.list_keys(scope)
        assert keys == ["token_a"]


def test_hashicorp_vault_provider_auth_modes():
    # 1. AppRole Auth
    prov_approle = HashiCorpVaultProvider(
        {
            "vault_url": "https://vault.corp:8200",
            "auth_mode": "approle",
            "role_id": "role-123",
            "secret_id": "secret-456",
            "approle_mount_point": "custom-approle",
            "namespace": "engineering",
        }
    )
    with patch("hvac.Client") as mock_hvac_cls:
        mock_c = MagicMock()
        mock_hvac_cls.return_value = mock_c
        prov_approle._create_client()
        mock_hvac_cls.assert_called_with(
            url="https://vault.corp:8200", namespace="engineering", verify=True
        )
        mock_c.auth.approle.login.assert_called_with(
            role_id="role-123", secret_id="secret-456", mount_point="custom-approle"
        )

    # 2. Kubernetes Service Account Auth
    prov_k8s = HashiCorpVaultProvider(
        {
            "vault_url": "https://vault.corp:8200",
            "auth_mode": "kubernetes",
            "k8s_role": "lake-of-tears-backend",
            "k8s_jwt": "jwt-token-string-xyz",
            "k8s_mount_point": "k8s-cluster",
        }
    )
    with patch("hvac.Client") as mock_hvac_cls:
        mock_c = MagicMock()
        mock_hvac_cls.return_value = mock_c
        prov_k8s._create_client()
        mock_c.auth.kubernetes.login.assert_called_with(
            role="lake-of-tears-backend", jwt="jwt-token-string-xyz", mount_point="k8s-cluster"
        )


def test_hashicorp_vault_provider_error_handling():
    import hvac.exceptions

    scope = SecretScope(
        id=uuid.uuid4(),
        name="vault-scope",
        backend_type="hashicorp_vault",
        provider_config={},
    )
    prov = HashiCorpVaultProvider({"vault_url": "https://vault.corp:8200", "token": "s.token"})

    mock_client = MagicMock()
    mock_client.secrets.kv.v2.read_secret_version.side_effect = hvac.exceptions.InvalidPath(
        "not found"
    )

    with patch.object(prov, "_get_client", return_value=mock_client):
        with pytest.raises(KeyError) as exc_info:
            prov.get_secret(scope, "missing_key")
        assert "not found in HashiCorp Vault" in str(exc_info.value)

    # Test connection sealed error
    mock_client.is_authenticated.return_value = True
    mock_client.sys.is_sealed.return_value = True
    with patch.object(prov, "_create_client", return_value=mock_client):
        ok, msg = prov.test_connection()
        assert ok is False
        assert "sealed" in msg

    # Test connection authentication failure
    mock_client.is_authenticated.return_value = False
    with patch.object(prov, "_create_client", return_value=mock_client):
        ok, msg = prov.test_connection()
        assert ok is False
        assert "authentication failed" in msg


# ── Google Cloud Secret Manager Unit Tests ────────────────────────────────────


def test_gcp_secret_manager_provider_operations():
    from google.api_core.exceptions import NotFound

    scope = SecretScope(
        id=uuid.uuid4(),
        name="gcp-scope",
        backend_type="gcp_secret_manager",
        provider_config={"project_id": "my-test-gcp-project", "prefix": "lake_of_tears"},
    )

    prov = GCPSecretManagerProvider(
        {
            "project_id": "my-test-gcp-project",
            "prefix": "lake_of_tears",
        }
    )

    mock_client = MagicMock()
    mock_version_resp = MagicMock()
    mock_version_resp.payload.data = b"gcp_secret_value_987"
    mock_client.access_secret_version.return_value = mock_version_resp

    mock_sec1 = MagicMock()
    mock_sec1.name = "projects/my-test-gcp-project/secrets/lake_of_tears__api_key"
    mock_sec1.labels = {"lake_scope": "gcp_scope"}

    mock_sec2 = MagicMock()
    mock_sec2.name = "projects/my-test-gcp-project/secrets/lake_of_tears__db_pass"
    mock_sec2.labels = {"lake_scope": "gcp_scope"}

    mock_client.list_secrets.return_value = [mock_sec1, mock_sec2]

    with patch.object(prov, "_get_client", return_value=mock_client):
        # 1. get_secret
        val = prov.get_secret(scope, "api_key")
        assert val == "gcp_secret_value_987"
        mock_client.access_secret_version.assert_called_with(
            request={
                "name": "projects/my-test-gcp-project/secrets/lake_of_tears__api_key/versions/latest"
            }
        )

        # 2. put_secret (existing secret version addition)
        prov.put_secret(scope, "api_key", "new_val")
        mock_client.add_secret_version.assert_called_with(
            request={
                "parent": "projects/my-test-gcp-project/secrets/lake_of_tears__api_key",
                "payload": {"data": b"new_val"},
            }
        )

        # 3. put_secret (creates secret if NotFound)
        mock_client.add_secret_version.side_effect = [
            NotFound("not found"),
            MagicMock(),
        ]
        prov.put_secret(scope, "brand_new_key", "first_value")
        mock_client.create_secret.assert_called()
        mock_client.add_secret_version.assert_called()

        # Reset side effect
        mock_client.add_secret_version.side_effect = None

        # 4. delete_secret
        res = prov.delete_secret(scope, "api_key")
        assert res is True
        mock_client.delete_secret.assert_called_with(
            request={"name": "projects/my-test-gcp-project/secrets/lake_of_tears__api_key"}
        )

        # 5. list_keys
        keys = prov.list_keys(scope)
        assert keys == ["api_key", "db_pass"]

        # 6. test_connection
        ok, msg = prov.test_connection()
        assert ok is True
        assert "Successfully connected" in msg
        assert "my-test-gcp-project" in msg


def test_gcp_secret_manager_provider_auth_and_errors():
    from google.api_core.exceptions import NotFound

    scope = SecretScope(
        id=uuid.uuid4(),
        name="gcp-scope",
        backend_type="gcp_secret_manager",
        provider_config={},
    )

    sa_json = {
        "type": "service_account",
        "project_id": "sa-inferred-project",
        "private_key_id": "key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...\n-----END PRIVATE KEY-----\n",
        "client_email": "lake-vault@sa-inferred-project.iam.gserviceaccount.com",
    }
    import json

    prov = GCPSecretManagerProvider(
        {
            "credentials_json": json.dumps(sa_json),
        }
    )
    # Verify project ID inferred from credentials JSON
    assert prov._resolve_project_id() == "sa-inferred-project"

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info"
    ) as mock_sa_info:
        with patch("google.cloud.secretmanager.SecretManagerServiceClient") as mock_sm_cls:
            prov._create_client()
            mock_sa_info.assert_called_once()
            mock_sm_cls.assert_called_once()

    # NotFound error on get_secret raises KeyError
    mock_client = MagicMock()
    mock_client.access_secret_version.side_effect = NotFound("Version latest not found")
    with patch.object(prov, "_get_client", return_value=mock_client):
        with pytest.raises(KeyError) as exc_info:
            prov.get_secret(scope, "nonexistent")
        assert "not found in Google Cloud Secret Manager" in str(exc_info.value)

    # Missing project ID causes test_connection failure
    empty_prov = GCPSecretManagerProvider({})
    with patch.dict("os.environ", {}, clear=True):
        ok, msg = empty_prov.test_connection({"project_id": ""})
        assert ok is False
        assert "Project ID is not configured" in msg or "missing" in msg


# ── Configuration Encryption & Masking for HashiCorp & GCP ───────────────────


def test_hashicorp_and_gcp_config_persistence_and_masking(db_session):
    super_user, _ = create_user_and_token(db_session, "superadmin2@example.com", role="superadmin")

    # HashiCorp Vault
    raw_hashi = {
        "vault_url": "https://vault.internal:8200",
        "auth_mode": "approle",
        "role_id": "role-uuid-1111",
        "secret_id": "secret-token-super-sensitive",
        "token": "s.vault-master-token",
        "mount_point": "secret",
        "kv_version": 2,
    }
    set_vault_provider_config(db_session, "hashicorp_vault", raw_hashi, user_id=super_user.id)
    saved_hashi = get_vault_provider_config(db_session, "hashicorp_vault")
    assert saved_hashi == raw_hashi
    masked_hashi = mask_vault_provider_config("hashicorp_vault", saved_hashi)
    assert masked_hashi["secret_id"].startswith("••••••••")
    assert masked_hashi["token"].startswith("••••••••")
    assert "secret-token-super-sensitive" not in masked_hashi["secret_id"]

    # GCP Secret Manager
    raw_gcp = {
        "project_id": "gcp-production-99",
        "credentials_json": '{"type": "service_account", "private_key": "MIIEvgIB..."}',
        "prefix": "lot_prod",
    }
    set_vault_provider_config(db_session, "gcp_secret_manager", raw_gcp, user_id=super_user.id)
    saved_gcp = get_vault_provider_config(db_session, "gcp_secret_manager")
    assert saved_gcp == raw_gcp
    masked_gcp = mask_vault_provider_config("gcp_secret_manager", saved_gcp)
    assert masked_gcp["credentials_json"].startswith("••••••••")
    assert "private_key" not in masked_gcp["credentials_json"]


# ── Admin API Tests for HashiCorp Vault & GCP ────────────────────────────────


def test_admin_hashicorp_and_gcp_api(client, db_session):
    super_user, super_token = create_user_and_token(
        db_session, "admin-phase2@example.com", role="superadmin"
    )
    headers_super = {"Authorization": f"Bearer {super_token}"}

    # 1. Configure HashiCorp Vault
    hashi_payload = {
        "config": {
            "vault_url": "https://vault.example.com:8200",
            "auth_mode": "token",
            "token": "s.my-top-secret-token",
            "mount_point": "secret",
            "kv_version": 2,
        }
    }
    res = client.put(
        "/api/admin/vault-providers/hashicorp_vault",
        headers=headers_super,
        json=hashi_payload,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "hashicorp_vault"
    assert data["is_configured"] is True
    assert data["config"]["vault_url"] == "https://vault.example.com:8200"
    assert data["config"]["token"].startswith("••••••••")

    # Update preserving masked token
    update_hashi = {
        "config": {
            "vault_url": "https://vault-renamed.example.com:8200",
            "token": data["config"]["token"],  # masked token string
        }
    }
    res = client.put(
        "/api/admin/vault-providers/hashicorp_vault",
        headers=headers_super,
        json=update_hashi,
    )
    assert res.status_code == 200
    stored_hashi = get_vault_provider_config(db_session, "hashicorp_vault")
    assert stored_hashi["token"] == "s.my-top-secret-token"
    assert stored_hashi["vault_url"] == "https://vault-renamed.example.com:8200"

    # Test connection for HashiCorp Vault
    with patch(
        "secrets_manager.hashicorp_vault.HashiCorpVaultProvider.test_connection",
        return_value=(True, "Vault ping ok"),
    ):
        res = client.post(
            "/api/admin/vault-providers/hashicorp_vault/test",
            headers=headers_super,
            json={},
        )
        assert res.status_code == 200
        assert res.json()["success"] is True
        assert res.json()["message"] == "Vault ping ok"

    # 2. Configure GCP Secret Manager
    gcp_payload = {
        "config": {
            "project_id": "gcp-lot-test",
            "credentials_json": '{"type": "service_account", "private_key": "SECRET"}',
        }
    }
    res = client.put(
        "/api/admin/vault-providers/gcp_secret_manager",
        headers=headers_super,
        json=gcp_payload,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "gcp_secret_manager"
    assert data["is_configured"] is True
    assert data["config"]["project_id"] == "gcp-lot-test"
    assert data["config"]["credentials_json"].startswith("••••••••")

    # Test connection for GCP Secret Manager
    with patch(
        "secrets_manager.gcp_sm.GCPSecretManagerProvider.test_connection",
        return_value=(True, "GCP Secret Manager ping ok"),
    ):
        res = client.post(
            "/api/admin/vault-providers/gcp_secret_manager/test",
            headers=headers_super,
            json={},
        )
        assert res.status_code == 200
        assert res.json()["success"] is True
        assert res.json()["message"] == "GCP Secret Manager ping ok"


# ── HashiCorp & GCP Backed Secret Scope Integration Tests ────────────────────


def test_hashicorp_and_gcp_backed_scope_integration(client, db_session):
    admin_user, admin_token = create_user_and_token(
        db_session, "hybridcloud@example.com", role="superadmin"
    )
    ws = create_workspace(db_session, "Hybrid Workspace", "hybrid-ws", admin_user)
    headers = {"Authorization": f"Bearer {admin_token}", "X-Workspace-Id": str(ws.id)}

    # Save Vault & GCP configurations in system settings
    set_vault_provider_config(
        db_session,
        "hashicorp_vault",
        {"vault_url": "https://vault.internal:8200", "token": "s.test-token"},
        user_id=admin_user.id,
    )
    set_vault_provider_config(
        db_session,
        "gcp_secret_manager",
        {"project_id": "hybrid-gcp-project"},
        user_id=admin_user.id,
    )

    # 1. Create HashiCorp Vault backed scope
    res = client.post(
        "/api/v1/secrets/scopes",
        headers=headers,
        json={"name": "vault-hybrid-scope", "backend_type": "hashicorp_vault"},
    )
    assert res.status_code == 201
    assert res.json()["backend_type"] == "hashicorp_vault"

    mock_vault_client = MagicMock()
    mock_vault_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"value": "vault-secret-val-abc"}}
    }
    mock_vault_client.secrets.kv.v2.list_secrets.return_value = {
        "data": {"keys": ["db_credential"]}
    }

    with patch(
        "secrets_manager.hashicorp_vault.HashiCorpVaultProvider._get_client",
        return_value=mock_vault_client,
    ):
        # Put secret
        res = client.post(
            "/api/v1/secrets/scopes/vault-hybrid-scope/keys",
            headers=headers,
            json={"key": "db_credential", "value": "vault-secret-val-abc"},
        )
        assert res.status_code == 201

        # List keys
        res = client.get("/api/v1/secrets/scopes/vault-hybrid-scope/keys", headers=headers)
        assert res.status_code == 200
        assert len(res.json()) == 1
        assert res.json()[0]["key"] == "db_credential"

        # Get secret
        res = client.get(
            "/api/v1/secrets/scopes/vault-hybrid-scope/keys/db_credential",
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()["value"] == "vault-secret-val-abc"

        # Delete secret
        res = client.delete(
            "/api/v1/secrets/scopes/vault-hybrid-scope/keys/db_credential",
            headers=headers,
        )
        assert res.status_code == 200

    # 2. Create GCP Secret Manager backed scope
    res = client.post(
        "/api/v1/secrets/scopes",
        headers=headers,
        json={"name": "gcp-hybrid-scope", "backend_type": "gcp_secret_manager"},
    )
    assert res.status_code == 201
    assert res.json()["backend_type"] == "gcp_secret_manager"

    mock_gcp_client = MagicMock()
    mock_ver = MagicMock()
    mock_ver.payload.data = b"gcp-secret-val-xyz"
    mock_gcp_client.access_secret_version.return_value = mock_ver

    mock_gcp_sec = MagicMock()
    mock_gcp_sec.name = "projects/hybrid-gcp-project/secrets/gcp_hybrid_scope__api_key"
    mock_gcp_sec.labels = {"lake_scope": "gcp_hybrid_scope"}
    mock_gcp_client.list_secrets.return_value = [mock_gcp_sec]

    with patch(
        "secrets_manager.gcp_sm.GCPSecretManagerProvider._get_client",
        return_value=mock_gcp_client,
    ):
        # Put secret
        res = client.post(
            "/api/v1/secrets/scopes/gcp-hybrid-scope/keys",
            headers=headers,
            json={"key": "api_key", "value": "gcp-secret-val-xyz"},
        )
        assert res.status_code == 201

        # List keys
        res = client.get("/api/v1/secrets/scopes/gcp-hybrid-scope/keys", headers=headers)
        assert res.status_code == 200
        assert len(res.json()) == 1
        assert res.json()[0]["key"] == "api_key"

        # Get secret
        res = client.get(
            "/api/v1/secrets/scopes/gcp-hybrid-scope/keys/api_key",
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()["value"] == "gcp-secret-val-xyz"

        # Delete secret
        res = client.delete(
            "/api/v1/secrets/scopes/gcp-hybrid-scope/keys/api_key",
            headers=headers,
        )
        assert res.status_code == 200

    # 3. Check all scopes audit contains both
    res = client.get("/api/admin/vault-providers/scopes", headers=headers)
    assert res.status_code == 200
    scope_names = [s["name"] for s in res.json()]
    assert "vault-hybrid-scope" in scope_names
    assert "gcp-hybrid-scope" in scope_names
