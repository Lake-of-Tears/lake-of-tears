from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# bcrypt only reads the first 72 bytes of a password, so a longer one would be silently cut and a
# change made past that point would not change anything. Passwords that long are refused instead.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72


def check_password_length(value: str) -> str:
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(
            f"Password is too long: the maximum is {PASSWORD_MAX_BYTES} bytes. Letters and digits "
            "use 1 byte each, accented letters 2, most other alphabets 3 and emoji 4."
        )
    return value


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long")
        return check_password_length(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        if len(v) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"New password must be at least {PASSWORD_MIN_LENGTH} characters long")
        return check_password_length(v)

    @model_validator(mode="after")
    def passwords_differ(self) -> Self:
        if (
            self.current_password
            and self.new_password
            and self.current_password == self.new_password
        ):
            raise ValueError("New password must be different from current password")
        return self


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    has_password: bool = True
    auth_providers: list[str] = []

    model_config = {"from_attributes": True}


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    display_name: str | None = None


class UpdateMeRequest(BaseModel):
    display_name: str | None = None


class CreateInvitationRequest(BaseModel):
    email: EmailStr
    role: str = "user"
    workspace_access: dict[str, str] = Field(default_factory=dict)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("user", "superadmin"):
            raise ValueError("Role must be 'user' or 'superadmin'")
        return v


class InvitationResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    workspace_access: dict | None = None
    status: str
    created_at: datetime
    accepted_at: datetime | None = None
    invited_by_email: str | None = None

    model_config = {"from_attributes": True}


class UserSessionResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str | None = None
    display_name: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime
    last_active_at: datetime
    expires_at: datetime
    is_revoked: bool
    revoked_at: datetime | None = None
    is_current: bool = False

    model_config = {"from_attributes": True}


# ── Personal Access Tokens ──────────────────────────────────────────────────


class PersonalAccessTokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    lifetime_days: int | None = Field(default=30, ge=1, le=3650)
    workspace_id: uuid.UUID | None = None
    workspace_ids: list[uuid.UUID] = Field(default_factory=list)
    workspace_access: dict[str, str] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Token name cannot be empty")
        return s

    @field_validator("workspace_access")
    @classmethod
    def validate_workspace_access(cls, v: dict[str, str]) -> dict[str, str]:
        valid_roles = {"read", "write", "admin"}
        cleaned: dict[str, str] = {}
        for raw_id, raw_role in v.items():
            try:
                ws_uuid = str(uuid.UUID(str(raw_id).strip()))
            except Exception:
                raise ValueError(f"Invalid workspace UUID: {raw_id}")
            role = str(raw_role).strip().lower()
            if role not in valid_roles:
                raise ValueError(f"Invalid access level '{role}'. Must be one of: {sorted(valid_roles)}")
            cleaned[ws_uuid] = role
        return cleaned

    @model_validator(mode="after")
    def validate_workspaces_and_scopes(self) -> Self:
        # Normalize single workspace_id into workspace_ids
        if not self.workspace_ids and self.workspace_id:
            self.workspace_ids = [self.workspace_id]

        if not self.scopes:
            raise ValueError("You must explicitly select at least one permission scope.")

        # Sync workspace_access and workspace_ids
        if self.workspace_access:
            access_wids = [uuid.UUID(k) for k in self.workspace_access.keys()]
            if not self.workspace_ids:
                self.workspace_ids = access_wids
            else:
                for wid in self.workspace_ids:
                    if str(wid) not in self.workspace_access:
                        self.workspace_access[str(wid)] = "read"
        elif self.workspace_ids:
            default_role = "read"
            if "admin" in self.scopes:
                default_role = "admin"
            elif "write" in self.scopes:
                default_role = "write"
            self.workspace_access = {str(wid): default_role for wid in self.workspace_ids}

        if not self.workspace_ids:
            raise ValueError("You must explicitly grant access to at least one workspace.")

        return self

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, v: list[str]) -> list[str]:
        valid_scopes = {"read", "write", "admin"}
        cleaned = []
        for s in v:
            item = s.strip().lower()
            if item not in valid_scopes:
                raise ValueError(f"Invalid scope '{item}'. Must be one of: {sorted(valid_scopes)}")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class PersonalAccessTokenCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    token: str
    token_prefix: str
    workspace_id: uuid.UUID | None = None
    workspace_ids: list[str] = Field(default_factory=list)
    workspace_access: dict[str, str] = Field(default_factory=dict)
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class PersonalAccessTokenResponse(BaseModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    workspace_id: uuid.UUID | None = None
    workspace_name: str | None = None
    workspace_ids: list[str] = Field(default_factory=list)
    workspace_names: list[str] = Field(default_factory=list)
    workspace_access: dict[str, str] = Field(default_factory=dict)
    workspace_details: list[dict[str, Any]] = Field(default_factory=list)
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_revoked: bool
    revoked_at: datetime | None = None

    model_config = {"from_attributes": True}


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
    global_role: str = "user"
    is_superadmin: bool = False
    joined_at: datetime

    model_config = {"from_attributes": True}


class CandidateMemberResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None = None

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
    is_shared: bool = False
    has_shared_catalogs: bool = False

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
    role: str = "read"

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in ("admin", "write", "read"):
            raise ValueError("role must be 'admin', 'write', or 'read'")
        return v


class UpdateMemberRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in ("admin", "write", "read"):
            raise ValueError("role must be 'admin', 'write', or 'read'")
        return v


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class WorkspaceShareRequest(BaseModel):
    note: str | None = None


class WorkspaceShareReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]


class WorkspaceShareResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    workspace_slug: str
    user_id: uuid.UUID
    user_email: str
    user_display_name: str | None = None
    status: Literal["pending", "approved", "rejected"]
    requested_at: datetime
    approved_at: datetime | None = None
    approved_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}


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
    column_defs: list[ColumnDef] | None = None

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
    vector_index: dict | None = None


class CatalogTableResponse(BaseModel):
    id: uuid.UUID
    schema_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    s3_path_pattern: str | None
    column_defs: list | None
    schema_drift: bool
    vector_index: dict | None = None
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
    mode: str = "read"
    schemas: list[CatalogSchemaResponse] = []


class OutboundCatalogAccessItem(BaseModel):
    access_id: uuid.UUID
    catalog_id: uuid.UUID
    catalog_name: str
    target_workspace_id: uuid.UUID
    target_workspace_name: str
    target_workspace_slug: str
    requester_name: str | None = None
    requester_email: str | None = None
    message: str | None = None
    status: str
    suspended: bool = False
    mode: str = "read"
    requested_at: datetime
    reviewed_at: datetime | None = None


class WorkspaceCatalogSettingsResponse(BaseModel):
    owned: list[CatalogResponse]
    shared: list[SharedCatalogSettingsItem]
    shared_with_others: list[OutboundCatalogAccessItem] = []


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
    requester_name: str | None = None
    requester_email: str | None = None
    message: str | None = None
    mode: str
    requested_at: datetime


class SystemSettingResponse(BaseModel):
    catalog_soft_delete_days: int
    workspace_inactive_grace_period_days: int
    session_inactivity_timeout_minutes: int
    session_max_hours: int
    gemini_api_key_configured: bool = False
    gemini_api_key_masked: str | None = None
    gemini_default_model: str | None = None


class UpdateSystemSettingsRequest(BaseModel):
    catalog_soft_delete_days: int | None = None
    workspace_inactive_grace_period_days: int | None = None
    session_inactivity_timeout_minutes: int | None = None
    session_max_hours: int | None = None
    gemini_api_key: str | None = None
    clear_gemini_api_key: bool = False
    gemini_default_model: str | None = None

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


class TestGeminiKeyRequest(BaseModel):
    api_key: str | None = None
    model: str | None = None


class TestGeminiKeyResponse(BaseModel):
    ok: bool
    model: str
    message: str


# ── Dashboards & Native Widgets ───────────────────────────────────────────────


class WidgetConfig(BaseModel):
    id: str | None = None
    title: str
    type: str  # 'kpi' | 'line' | 'bar' | 'area' | 'pie' | 'table'
    dataset_id: str | int | None = None
    metric: str | None = None
    metric_agg: str | None = "sum"
    dimension: str | None = None
    time_range: str | None = "all"
    width: str = "half"  # 'half' | 'full'


class CreateDashboardRequest(BaseModel):
    title: str
    description: str | None = None
    is_published: bool = True
    template: str | None = "blank"  # 'blank' | 'executive' | 'traffic'


class LinkDashboardRequest(BaseModel):
    superset_dashboard_id: int
    title: str | None = None
    description: str | None = None
    is_published: bool = True


class UpdateDashboardRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    is_published: bool | None = None
    layout_config: list[dict[str, Any]] | None = None


class DashboardResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    slug: str
    description: str | None
    is_published: bool
    layout_config: list[dict[str, Any]] = []
    charts_count: int = 0
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WidgetDataResponse(BaseModel):
    widget_id: str
    type: str
    title: str
    data: dict[str, Any]


class DashboardStatsResponse(BaseModel):
    total_dashboards: int
    published_dashboards: int
    total_charts: int
    superset_online: bool


# ── AI Query, SQL AI Assistant & Genie Conversational Agent ───────────────────


class SqlExecuteRequest(BaseModel):
    sql: str
    read_only: bool = True
    max_ai_rows: int = 50


class SqlExecuteResponse(BaseModel):
    ok: bool
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    execution_ms: float = 0.0
    ai_calls_made: int = 0
    registered_views: list[str] = []
    error: str | None = None


class SqlGenerateRequest(BaseModel):
    prompt: str
    current_sql: str | None = None
    model: str | None = None


class SqlGenerateResponse(BaseModel):
    ok: bool
    sql: str = ""
    explanation: str = ""
    tables_used: list[str] = []
    ai_functions_used: list[str] = []
    error: str | None = None


class SqlFixRequest(BaseModel):
    sql: str
    error: str
    model: str | None = None


class SqlFixResponse(BaseModel):
    ok: bool
    fixed_sql: str = ""
    explanation: str = ""
    error: str | None = None


class SqlExplainRequest(BaseModel):
    sql: str
    model: str | None = None


class SqlExplainResponse(BaseModel):
    ok: bool
    summary: str = ""
    steps: list[str] = []
    tables_referenced: list[str] = []
    ai_functions_used: list[str] = []
    error: str | None = None


class ChatMessageItem(BaseModel):
    role: str  # 'user' | 'assistant'
    content: str
    sql: str | None = None


class GenieChatRequest(BaseModel):
    question: str
    mode: str = "genie"  # 'genie' | 'hybrid' | 'rag'
    history: list[ChatMessageItem] = []
    model: str | None = None
    request_id: str | None = None
    workspace_slug: str | None = None
    workspace_id: str | None = None


class GenieChartConfig(BaseModel):
    type: str = "table"  # 'bar' | 'line' | 'area' | 'pie' | 'kpi' | 'table'
    title: str = ""
    x_column: str | None = None
    y_columns: list[str] = []


class GenieChatResponse(BaseModel):
    ok: bool
    mode: str = "genie"
    answer: str = ""
    sql: str | None = None
    self_corrected: bool = False
    initial_error: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    execution_ms: float = 0.0
    chart_config: GenieChartConfig | None = None
    snippets: list[dict[str, Any]] = []
    tables_used: list[str] = []
    cancelled: bool = False
    error: str | None = None


class AnomalyDetectRequest(BaseModel):
    source: str | None = None
    table_slug: str | None = None
    contamination: float = 0.05
