"""Pydantic specification models for Lake Asset Bundles (LABs)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BundleMetadata(BaseModel):
    """Core bundle metadata defining identifier, version, and ownership."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Unique bundle identifier slug, e.g. 'customer360-etl'")
    version: str = Field("0.1.0", description="Semver version string")
    author: str | None = Field(None, description="Author email or team identifier")
    description: str | None = Field(
        None, description="High-level description of bundle architecture"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Bundle name cannot be empty")
        return v


class VariableSpec(BaseModel):
    """Specification of a declared bundle template variable."""

    model_config = ConfigDict(extra="ignore")

    default: Any = Field(None, description="Default value if not overridden by target or CLI")
    type: str = Field("string", description="Variable type: string, int, float, bool, list")
    description: str | None = Field(None, description="Variable documentation")


class TargetSpec(BaseModel):
    """Target deployment environment specification (e.g. dev, staging, prod)."""

    model_config = ConfigDict(extra="ignore")

    workspace: str | None = Field(None, description="Target workspace ID or slug")
    default_catalog: str | None = Field(None, description="Default catalog for unqualified tables")
    compute_cluster: str | None = Field(None, description="Target compute engine/cluster")
    variables: dict[str, Any] = Field(
        default_factory=dict, description="Variable overrides for target"
    )
    environment_variables: dict[str, str] = Field(
        default_factory=dict, description="Environment variables injected into tasks"
    )
    tags: dict[str, str] = Field(default_factory=dict, description="Environment metadata tags")

    @field_validator("variables", "environment_variables", "tags", mode="before")
    @classmethod
    def parse_dict(cls, v: Any) -> dict:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        return {}


class ColumnDef(BaseModel):
    """Column definition for lakehouse tables."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Column name")
    type: str = Field(
        "VARCHAR", description="DuckDB SQL column type (e.g. VARCHAR, BIGINT, TIMESTAMP)"
    )
    description: str | None = Field(None, description="Semantic description of column")
    nullable: bool = Field(True, description="Whether column accepts NULL values")


class CatalogResource(BaseModel):
    """Catalog asset declaration."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Catalog name")
    description: str | None = Field(None, description="Catalog description")
    schemas: list[str] = Field(
        default_factory=list, description="Optional list of schema names to provision automatically"
    )

    @field_validator("schemas", mode="before")
    @classmethod
    def parse_schemas_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = v[1:-1].strip()
            if not v:
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class SchemaResource(BaseModel):
    """Schema asset declaration within a catalog."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Schema name")
    catalog: str | None = Field(None, description="Parent catalog name")
    tier: str = Field("custom", description="Medallion tier: bronze, silver, gold, or custom")
    description: str | None = Field(None, description="Schema description")


class TableResource(BaseModel):
    """Table asset declaration with column definitions and storage pattern."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(..., description="Table name")
    catalog: str | None = Field(None, description="Parent catalog name")
    schema_name: str | None = Field(None, alias="schema", description="Parent schema name")
    description: str | None = Field(None, description="Table description")
    format: str = Field("parquet", description="Storage format: parquet, csv, delta")
    s3_path_pattern: str | None = Field(None, description="MinIO/S3 glob pattern or path")
    columns: list[ColumnDef] = Field(default_factory=list, description="Column definitions")
    partition_by: list[str] = Field(default_factory=list, description="Partition columns")
    primary_key: list[str] = Field(default_factory=list, description="Primary key columns")

    @field_validator("partition_by", "primary_key", mode="before")
    @classmethod
    def parse_str_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = v[1:-1].strip()
            if not v:
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class VectorIndexResource(BaseModel):
    """Vector index declaration for semantic search and RAG."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Vector index name")
    table: str = Field(..., description="Target table (format: 'catalog.schema.table' or 'table')")
    column: str = Field(..., description="Text column name to embed and index")
    metric: str = Field("cosine", description="Distance metric: cosine, l2, or ip")
    dimensions: int | None = Field(None, description="Vector dimensions (e.g. 768, 1536)")
    model: str | None = Field(None, description="Embedding model identifier")


class PipelineTask(BaseModel):
    """Individual task within a DAG pipeline."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Task identifier within DAG")
    type: str = Field(
        "duckdb_sql", description="Task type: duckdb_sql, python_script, notebook, bash"
    )
    entrypoint: str = Field(..., description="Path to script, notebook, or SQL query file")
    requirements: str | None = Field(
        None, description="Path to requirements.txt or comma-separated packages"
    )
    upstream_tasks: list[str] = Field(
        default_factory=list, description="Names of upstream dependency tasks"
    )
    timeout_seconds: int = Field(3600, description="Task execution timeout in seconds")
    retry_count: int = Field(0, description="Retries on failure")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Runtime parameters passed to task"
    )
    resources: dict[str, str] = Field(
        default_factory=dict,
        description="Optional KubernetesExecutor compute resource overrides (cpu, memory, cpu_limit, memory_limit)",
    )

    @field_validator("upstream_tasks", mode="before")
    @classmethod
    def parse_upstream_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = v[1:-1].strip()
            if not v:
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class PipelineResource(BaseModel):
    """Multi-task Airflow pipeline / DAG declaration."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Pipeline / DAG identifier")
    description: str | None = Field(None, description="Pipeline purpose description")
    schedule: str | None = Field(None, description="Cron schedule expression (e.g. '0 2 * * *')")
    webhook_enabled: bool = Field(
        False, description="Whether pipeline accepts direct inbound webhook triggers"
    )
    compute: str | None = Field(None, description="Target compute cluster/worker queue")
    tasks: list[PipelineTask] = Field(default_factory=list, description="Ordered or DAG task list")
    tags: list[str] = Field(default_factory=list, description="Pipeline tags")

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = v[1:-1].strip()
            if not v:
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class DashboardWidget(BaseModel):
    """Visual widget declaration inside a dashboard."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., description="Widget title")
    type: str = Field("table", description="Widget type: table, bar, line, pie, metric, markdown")
    query: str | None = Field(None, description="SQL query powering widget")
    width: int = Field(6, description="Grid width in 12-column layout (1-12)")
    height: int = Field(4, description="Grid height units")


class DashboardResource(BaseModel):
    """Dashboard declaration with visual widgets."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="Dashboard identifier")
    description: str | None = Field(None, description="Dashboard description")
    widgets: list[DashboardWidget] = Field(default_factory=list, description="Dashboard widgets")


class ResourceInventory(BaseModel):
    """Container for all declared lakehouse resources in a bundle."""

    model_config = ConfigDict(extra="ignore")

    catalogs: list[CatalogResource] = Field(default_factory=list)
    schemas: list[SchemaResource] = Field(default_factory=list)
    tables: list[TableResource] = Field(default_factory=list)
    vector_indexes: list[VectorIndexResource] = Field(default_factory=list)
    pipelines: list[PipelineResource] = Field(default_factory=list)
    dashboards: list[DashboardResource] = Field(default_factory=list)


class BundleSpec(BaseModel):
    """Unified specification model for Lake Asset Bundles (LABs)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    bundle: BundleMetadata = Field(..., description="Bundle metadata")
    variables: dict[str, VariableSpec] = Field(
        default_factory=dict, description="Declared variables"
    )
    targets: dict[str, TargetSpec] = Field(default_factory=dict, description="Deployment targets")
    resources: ResourceInventory = Field(
        default_factory=ResourceInventory, description="Bundle resources"
    )


# ── REST API Request & Response Schemas ────────────────────────────────────────


class BundleValidateRequest(BaseModel):
    """Payload for validating a bundle manifest."""

    model_config = ConfigDict(extra="ignore")

    manifest_content: str = Field(
        ..., description="Raw bundle manifest string (TOON, YAML, or JSON)"
    )
    manifest_format: str = Field("toon", description="Manifest format: toon, yaml, yml, json")
    target: str | None = Field(None, description="Optional target environment name to evaluate")
    variable_overrides: dict[str, Any] | None = Field(
        default=None, description="Dynamic variable overrides"
    )
    workspace_slug: str | None = Field(None, description="Target workspace slug")
    workspace_id: str | None = Field(None, description="Target workspace UUID")


class BundleValidationResponse(BaseModel):
    """Result of bundle manifest validation and dry-run reconciliation diff."""

    model_config = ConfigDict(extra="ignore")

    valid: bool = Field(..., description="Whether the bundle manifest is valid and deployable")
    bundle_name: str = Field(..., description="Validated bundle name")
    target: str = Field("default", description="Evaluated target name")
    errors: list[str] = Field(default_factory=list, description="Validation error messages")
    warnings: list[str] = Field(default_factory=list, description="Validation warning messages")
    diff: dict[str, Any] = Field(
        default_factory=dict,
        description="Reconciliation diff of resources to create, update, delete",
    )
    resolved_bundle: dict[str, Any] | None = Field(
        default=None, description="Resolved bundle specification payload"
    )


class BundleDeployRequest(BaseModel):
    """Payload for deploying a Lake Asset Bundle."""

    model_config = ConfigDict(extra="ignore")

    manifest_content: str = Field(
        ..., description="Raw bundle manifest string (TOON, YAML, or JSON)"
    )
    manifest_format: str = Field("toon", description="Manifest format: toon, yaml, yml, json")
    target: str | None = Field(None, description="Target environment name")
    variable_overrides: dict[str, Any] | None = Field(
        default=None, description="Dynamic variable overrides"
    )
    commit_hash: str | None = Field(None, description="Optional Git commit hash or version tag")
    files: dict[str, str] | None = Field(
        default=None,
        description="Optional mapping of relative file paths to content for artifact sync",
    )
    dry_run: bool = Field(False, description="Simulate deployment without persisting changes")
    workspace_slug: str | None = Field(None, description="Target workspace slug")
    workspace_id: str | None = Field(None, description="Target workspace UUID")


class BundleDeployResponse(BaseModel):
    """Response returned upon bundle deployment execution."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID | None = Field(None, description="Workspace bundle deployment UUID")
    bundle_name: str = Field(..., description="Deployed bundle name")
    target: str = Field("default", description="Deployed target environment")
    status: str = Field(..., description="Deployment status: active, deploying, failed")
    resources_map: dict[str, list[str]] = Field(
        default_factory=dict, description="Map of resource types to provisioned IDs"
    )
    message: str = Field(..., description="Human-readable deployment summary")
    deployed_at: datetime | None = Field(default=None, description="Deployment timestamp")
    error_message: str | None = Field(None, description="Error details if deployment failed")


class BundleItemResponse(BaseModel):
    """Summary item for bundle listings."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    workspace_id: uuid.UUID
    bundle_name: str
    target: str
    status: str
    version: str | None = None
    manifest_format: str
    commit_hash: str | None = None
    resources_count: int = 0
    resources_map: dict[str, list[Any]] = Field(default_factory=dict)
    catalogs_count: int = 0
    tables_count: int = 0
    pipelines_count: int = 0
    dashboards_count: int = 0
    deployed_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    last_deployed_at: datetime | None = None


class BundleListResponse(BaseModel):
    """Paginated or listed collection of deployed workspace bundles."""

    model_config = ConfigDict(extra="ignore")

    bundles: list[BundleItemResponse] = Field(default_factory=list)
    total: int = 0


class BundleDetailResponse(BaseModel):
    """Full detail view of a deployed workspace bundle."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    workspace_id: uuid.UUID
    bundle_name: str
    target: str
    status: str
    version: str | None = None
    manifest_format: str
    manifest_raw: str
    commit_hash: str | None = None
    resources_map: dict[str, list[Any]] = Field(default_factory=dict)
    resources_count: int = 0
    catalogs_count: int = 0
    tables_count: int = 0
    pipelines_count: int = 0
    dashboards_count: int = 0
    deployed_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    last_deployed_at: datetime | None = None
    error_message: str | None = None
    drift: dict[str, Any] | None = None


class BundleRunRequest(BaseModel):
    """Payload to trigger an ad-hoc pipeline execution for a deployed bundle."""

    model_config = ConfigDict(extra="ignore")

    pipeline_name: str | None = Field(
        None, description="Optional specific pipeline identifier to run; defaults to first"
    )
    conf: dict[str, Any] | None = Field(
        default=None, description="Optional runtime parameters or Airflow DAG run conf"
    )


class BundleRunResponse(BaseModel):
    """Response returned upon triggering a bundle pipeline execution."""

    model_config = ConfigDict(extra="ignore")

    bundle_id: uuid.UUID
    pipeline_name: str
    dag_id: str
    dag_run_id: str
    status: str
    execution_date: str | None = None


class BundleDestroyResponse(BaseModel):
    """Response returned upon tearing down / deleting a deployed bundle."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    bundle_name: str
    target: str
    status: str = "destroyed"
    purged_resources: dict[str, list[str]] = Field(default_factory=dict)
    message: str
