from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    display_name: str | None = None


class UpdateMeRequest(BaseModel):
    display_name: str | None = None


# ── Workspaces ────────────────────────────────────────────────────────────────


class CreateWorkspaceRequest(BaseModel):
    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Workspace name cannot be empty")
        return v


class WorkspaceMemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    joined_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    created_at: datetime
    is_system: bool = False
    status: str = "active"
    # caller's role within this workspace (populated on the fly)
    my_role: str | None = None

    model_config = {"from_attributes": True}


class WorkspaceAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    is_system: bool
    status: Literal["active", "inactive"]
    deleted_at: datetime | None
    scheduled_purge_at: datetime | None
    member_count: int
    owned_catalog_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DeleteWorkspaceRequest(BaseModel):
    mode: str  # 'hard' | 'soft'
    confirm_name: str | None = None

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v: str) -> str:
        if v not in ("hard", "soft"):
            raise ValueError("mode must be 'hard' or 'soft'")
        return v


class AddMemberRequest(BaseModel):
    email: EmailStr
    role: str = "user"

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        return v


class UpdateMemberRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        return v


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    description: str | None = None


# ── Catalogs ──────────────────────────────────────────────────────────────────


class CreateCatalogRequest(BaseModel):
    name: str
    description: str | None = None
    workspace_id: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Catalog name cannot be empty")
        return v


class UpdateCatalogRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class CreateSchemaRequest(BaseModel):
    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Schema name cannot be empty")
        return v


class UpdateSchemaRequest(BaseModel):
    description: str | None = None


class ColumnDef(BaseModel):
    name: str
    type: str
    description: str | None = None
    deprecated: bool = False


class CreateTableRequest(BaseModel):
    name: str
    description: str | None = None
    s3_path_pattern: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Table name cannot be empty")
        return v


class UpdateTableRequest(BaseModel):
    description: str | None = None
    s3_path_pattern: str | None = None
    column_defs: list[ColumnDef] | None = None


class CatalogTableResponse(BaseModel):
    id: uuid.UUID
    schema_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    s3_path_pattern: str | None
    column_defs: list | None
    schema_drift: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CatalogSchemaResponse(BaseModel):
    id: uuid.UUID
    catalog_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    tier: str
    created_at: datetime
    tables: list[CatalogTableResponse] = []

    model_config = {"from_attributes": True}


class CatalogResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    owner_workspace_id: uuid.UUID
    owner_workspace_name: str | None = None
    created_at: datetime
    deleted_at: datetime | None = None
    scheduled_purge_at: datetime | None = None
    is_default: bool = False
    schemas: list[CatalogSchemaResponse] = []
    my_access: str | None = None  # 'owner' | 'write' | 'read' | None

    model_config = {"from_attributes": True}


class PurgeCatalogRequest(BaseModel):
    confirm_name: str


class RequestAccessRequest(BaseModel):
    mode: str = "read"
    message: str | None = None
    workspace_id: str | None = None

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v: str) -> str:
        if v not in ("read", "write"):
            raise ValueError("mode must be 'read' or 'write'")
        return v


class ReviewAccessRequest(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def valid_status(cls, v: str) -> str:
        if v not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        return v


class CatalogAccessResponse(BaseModel):
    id: uuid.UUID
    catalog_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str | None = None
    mode: str
    status: str
    suspended: bool = False
    requested_by_email: str | None = None
    requested_at: datetime
    reviewed_at: datetime | None

    model_config = {"from_attributes": True}


class SharedCatalogSettingsItem(BaseModel):
    access_id: uuid.UUID
    catalog_id: uuid.UUID
    catalog_name: str
    owner_workspace_id: uuid.UUID
    owner_workspace_name: str | None = None
    status: str
    suspended: bool = False


class WorkspaceCatalogSettingsResponse(BaseModel):
    owned: list[CatalogResponse]
    shared: list[SharedCatalogSettingsItem]


class CatalogDirectoryItem(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    owner_workspace_id: uuid.UUID
    owner_workspace_name: str | None
    schema_count: int
    access_status: str  # 'owned' | 'approved' | 'pending' | 'none'
    access_id: uuid.UUID | None = None


class InboundAccessRequest(BaseModel):
    access_id: uuid.UUID
    catalog_id: uuid.UUID
    catalog_name: str
    requesting_workspace_id: uuid.UUID
    requesting_workspace_name: str | None
    mode: str
    requested_at: datetime


class SystemSettingResponse(BaseModel):
    catalog_soft_delete_days: int
    workspace_inactive_grace_period_days: int
    session_inactivity_timeout_minutes: int
    session_max_hours: int


class UpdateSystemSettingsRequest(BaseModel):
    catalog_soft_delete_days: int | None = None
    workspace_inactive_grace_period_days: int | None = None
    session_inactivity_timeout_minutes: int | None = None
    session_max_hours: int | None = None

    @field_validator("session_inactivity_timeout_minutes")
    @classmethod
    def valid_session_timeout(cls, v: int | None) -> int | None:
        if v is not None and v != 0 and (v < 15 or v > 480):
            raise ValueError("Inactivity timeout must be 0 (disabled) or 15–480 minutes")
        return v

    @field_validator("session_max_hours")
    @classmethod
    def valid_session_max(cls, v: int | None) -> int | None:
        if v is not None and (v < 1 or v > 168):
            raise ValueError("Max session length must be 1–168 hours")
        return v

    @field_validator("catalog_soft_delete_days")
    @classmethod
    def valid_catalog_days(cls, v: int | None) -> int | None:
        if v is not None and (v < 0 or v > 365):
            raise ValueError("Days must be between 0 and 365")
        return v

    @field_validator("workspace_inactive_grace_period_days")
    @classmethod
    def valid_workspace_days(cls, v: int | None) -> int | None:
        if v is not None and (v < 0 or v > 365):
            raise ValueError("Days must be between 0 and 365")
        return v
