from __future__ import annotations

import logging
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError
from models import SecretScope

from secrets_manager.base import BaseSecretProvider

logger = logging.getLogger("lake.secrets.aws_sm")


class AWSSecretsManagerProvider(BaseSecretProvider):
    """AWS Secrets Manager provider with IAM role / IRSA and explicit credentials support."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.region_name = self.config.get("region_name", "us-east-1").strip()
        self.access_key_id = self.config.get("aws_access_key_id", "").strip()
        self.secret_access_key = self.config.get("aws_secret_access_key", "").strip()
        self.session_token = self.config.get("aws_session_token", "").strip()
        self.role_arn = self.config.get("role_arn", "").strip()
        self.default_prefix = self.config.get("prefix", "").strip()
        self._client = None

    def _get_client(self, region_override: str | None = None):
        region = region_override or self.region_name or "us-east-1"
        if self._client and not region_override:
            return self._client

        kwargs: dict[str, Any] = {"region_name": region}
        if self.access_key_id and self.secret_access_key:
            kwargs["aws_access_key_id"] = self.access_key_id
            kwargs["aws_secret_access_key"] = self.secret_access_key
            if self.session_token:
                kwargs["aws_session_token"] = self.session_token

        client = boto3.client("secretsmanager", **kwargs)

        if self.role_arn:
            sts = boto3.client("sts", **kwargs)
            assumed = sts.assume_role(
                RoleArn=self.role_arn,
                RoleSessionName="LakeOfTearsSecretManagerSession",
            )
            creds = assumed["Credentials"]
            client = boto3.client(
                "secretsmanager",
                region_name=region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )

        if not region_override:
            self._client = client
        return client

    def _resolve_key_name(self, scope: SecretScope, key: str) -> str:
        scope_conf = scope.provider_config or {}
        prefix = scope_conf.get("prefix", self.default_prefix)
        if prefix:
            prefix = prefix.rstrip("/") + "/"
            return f"{prefix}{key}"
        return f"{scope.name}/{key}"

    def _strip_key_name(self, scope: SecretScope, full_name: str) -> str | None:
        scope_conf = scope.provider_config or {}
        prefix = scope_conf.get("prefix", self.default_prefix)
        if prefix:
            prefix = prefix.rstrip("/") + "/"
            if full_name.startswith(prefix):
                return full_name[len(prefix) :]
            return None
        scope_prefix = f"{scope.name}/"
        if full_name.startswith(scope_prefix):
            return full_name[len(scope_prefix) :]
        return None

    def get_secret(self, scope: SecretScope, key: str) -> str:
        scope_conf = scope.provider_config or {}
        region = scope_conf.get("region_name") or self.region_name
        client = self._get_client(region)
        secret_id = self._resolve_key_name(scope, key)

        try:
            resp = client.get_secret_value(SecretId=secret_id)
            if "SecretString" in resp:
                return resp["SecretString"]
            if "SecretBinary" in resp:
                import base64

                return base64.b64encode(resp["SecretBinary"]).decode("ascii")
            return ""
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ResourceNotFoundException", "DecryptionFailureException"):
                raise KeyError(
                    f"Secret '{key}' ({secret_id}) not found in AWS Secrets Manager: {e}"
                )
            raise

    def put_secret(
        self, scope: SecretScope, key: str, value: str, user_id: uuid.UUID | None = None
    ) -> None:
        scope_conf = scope.provider_config or {}
        region = scope_conf.get("region_name") or self.region_name
        client = self._get_client(region)
        secret_id = self._resolve_key_name(scope, key)

        try:
            client.put_secret_value(SecretId=secret_id, SecretString=value)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                client.create_secret(
                    Name=secret_id,
                    SecretString=value,
                    Description=f"Managed by Lake of Tears (Scope: {scope.name})",
                )
            else:
                raise

    def delete_secret(self, scope: SecretScope, key: str) -> bool:
        scope_conf = scope.provider_config or {}
        region = scope_conf.get("region_name") or self.region_name
        client = self._get_client(region)
        secret_id = self._resolve_key_name(scope, key)

        try:
            client.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                return False
            raise

    def list_keys(self, scope: SecretScope) -> list[str]:
        scope_conf = scope.provider_config or {}
        region = scope_conf.get("region_name") or self.region_name
        client = self._get_client(region)

        keys = []
        paginator = client.get_paginator("list_secrets")
        for page in paginator.paginate():
            for item in page.get("SecretList", []):
                name = item.get("Name", "")
                stripped = self._strip_key_name(scope, name)
                if stripped:
                    keys.append(stripped)
        return sorted(keys)

    def test_connection(self, config: dict[str, Any] | None = None) -> tuple[bool, str]:
        test_conf = {**self.config, **(config or {})}
        region = test_conf.get("region_name", self.region_name or "us-east-1").strip()
        kwargs: dict[str, Any] = {"region_name": region}
        ak = test_conf.get("aws_access_key_id", "").strip()
        sk = test_conf.get("aws_secret_access_key", "").strip()
        if ak and sk:
            kwargs["aws_access_key_id"] = ak
            kwargs["aws_secret_access_key"] = sk

        try:
            client = boto3.client("secretsmanager", **kwargs)
            client.list_secrets(MaxResults=1)
            return True, f"Successfully connected to AWS Secrets Manager in {region}"
        except Exception as e:
            logger.warning("AWS Secrets Manager test_connection failed: %s", e)
            return False, f"AWS Secrets Manager connection failed: {e}"
