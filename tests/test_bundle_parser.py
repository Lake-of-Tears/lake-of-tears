"""Unit and integration tests for Lake Asset Bundles (LABs) manifest parser and schemas."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.bundles import (
    BundleParseError,
    BundleValidationError,
    BundleVariableError,
    dump_bundle_to_str,
    parse_bundle_file,
    parse_bundle_str,
    resolve_bundle,
)

SAMPLE_TOON_BUNDLE = """
bundle:
  name: customer360-etl
  version: 1.2.0
  author: data-platform@lakeoftears.io
  description: Customer 360 medallion analytics lakehouse bundle

variables:
  raw_bucket:
    default: datalake-bronze
    type: string
    description: Primary S3 ingestion bucket
  batch_size:
    default: 50000
    type: int

targets:
  dev:
    workspace: dev-workspace
    default_catalog: main_dev
    variables:
      raw_bucket: datalake-dev
  prod:
    workspace: prod-workspace
    default_catalog: main_prod
    variables:
      raw_bucket: datalake-prod

resources:
  catalogs[1]{name,description}:
    main_lake,Primary analytics lakehouse catalog

  schemas[3]{catalog,name,tier,description}:
    main_lake,bronze,bronze,Raw ingested customer events
    main_lake,silver,silver,Cleaned and deduplicated customer entities
    main_lake,gold,gold,Aggregated revenue and lifetime value marts

  tables[2]{name,catalog,schema,format,s3_path_pattern}:
    raw_customers,main_lake,bronze,parquet,"s3://${var.raw_bucket}/customers/*.parquet"
    dim_customers,main_lake,silver,parquet,"s3://${var.raw_bucket}/silver/dim_customers/*.parquet"

  vector_indexes[1]{name,table,column,metric}:
    customer_notes_idx,main_lake.silver.dim_customers,notes,cosine

  pipelines:
    daily_customer_pipeline:
      description: Daily ingestion and medallion dimensional build
      schedule: "0 2 * * *"
      tasks[3]{name,type,entrypoint,upstream_tasks}:
        extract_events,duckdb_sql,sql/extract.sql,[]
        clean_silver,duckdb_sql,sql/silver_transform.sql,[extract_events]
        build_gold_marts,duckdb_sql,sql/gold_marts.sql,[clean_silver]

  dashboards:
    executive_overview:
      description: Executive KPI Dashboard
      widgets:
        revenue_metric:
          type: metric
          query: "SELECT sum(ltv) FROM main_lake.gold.customer_ltv"
          width: 4
          height: 2
        customer_table:
          type: table
          query: "SELECT * FROM main_lake.silver.dim_customers LIMIT 100"
          width: 8
          height: 4
"""

SAMPLE_YAML_BUNDLE = """
bundle:
  name: telemetry-lake
  version: 0.5.0
  description: Device telemetry monitoring bundle

variables:
  iot_topic:
    default: devices/telemetry
    type: string

targets:
  staging:
    workspace: ws-staging
    variables:
      iot_topic: devices/staging-telemetry

resources:
  catalogs:
    telemetry_cat:
      description: IoT Telemetry catalog
  schemas:
    raw:
      catalog: telemetry_cat
      tier: bronze
  tables:
    device_events:
      catalog: telemetry_cat
      schema: raw
      format: parquet
      s3_path_pattern: "s3://telemetry/${var.iot_topic}/*.parquet"
      columns:
        device_id: VARCHAR
        temperature: DOUBLE
        recorded_at: TIMESTAMP
  pipelines:
    telemetry_dag:
      schedule: "@hourly"
      tasks:
        ingest:
          type: duckdb_sql
          entrypoint: sql/ingest.sql
        aggregate:
          type: duckdb_sql
          entrypoint: sql/agg.sql
          upstream_tasks: [ingest]
"""


def test_parse_toon_bundle():
    """Test parsing a comprehensive TOON-formatted manifest."""
    spec = parse_bundle_str(SAMPLE_TOON_BUNDLE, format="toon")

    assert spec.bundle.name == "customer360-etl"
    assert spec.bundle.version == "1.2.0"
    assert spec.bundle.author == "data-platform@lakeoftears.io"

    # Variables
    assert "raw_bucket" in spec.variables
    assert spec.variables["raw_bucket"].default == "datalake-bronze"
    assert spec.variables["batch_size"].default == 50000

    # Targets
    assert "dev" in spec.targets
    assert spec.targets["dev"].workspace == "dev-workspace"
    assert spec.targets["dev"].variables["raw_bucket"] == "datalake-dev"
    assert "prod" in spec.targets
    assert spec.targets["prod"].workspace == "prod-workspace"

    # Resources
    assert len(spec.resources.catalogs) == 1
    assert spec.resources.catalogs[0].name == "main_lake"

    assert len(spec.resources.schemas) == 3
    schema_names = [s.name for s in spec.resources.schemas]
    assert schema_names == ["bronze", "silver", "gold"]

    assert len(spec.resources.tables) == 2
    assert spec.resources.tables[0].name == "raw_customers"
    assert spec.resources.tables[0].schema_name == "bronze"

    assert len(spec.resources.vector_indexes) == 1
    assert spec.resources.vector_indexes[0].metric == "cosine"

    assert len(spec.resources.pipelines) == 1
    pipe = spec.resources.pipelines[0]
    assert pipe.name == "daily_customer_pipeline"
    assert len(pipe.tasks) == 3
    assert pipe.tasks[0].name == "extract_events"
    assert pipe.tasks[1].upstream_tasks == ["extract_events"]
    assert pipe.tasks[2].upstream_tasks == ["clean_silver"]

    assert len(spec.resources.dashboards) == 1
    dash = spec.resources.dashboards[0]
    assert dash.name == "executive_overview"
    assert len(dash.widgets) == 2


def test_parse_yaml_bundle():
    """Test parsing a YAML manifest with dict-based resource definitions."""
    spec = parse_bundle_str(SAMPLE_YAML_BUNDLE, format="yaml")

    assert spec.bundle.name == "telemetry-lake"
    assert spec.bundle.version == "0.5.0"
    assert len(spec.resources.catalogs) == 1
    assert spec.resources.catalogs[0].name == "telemetry_cat"
    assert len(spec.resources.schemas) == 1
    assert spec.resources.schemas[0].name == "raw"
    assert spec.resources.schemas[0].catalog == "telemetry_cat"

    assert len(spec.resources.tables) == 1
    table = spec.resources.tables[0]
    assert table.name == "device_events"
    assert len(table.columns) == 3
    col_names = [c.name for c in table.columns]
    assert col_names == ["device_id", "temperature", "recorded_at"]

    assert len(spec.resources.pipelines) == 1
    pipeline = spec.resources.pipelines[0]
    assert pipeline.name == "telemetry_dag"
    assert len(pipeline.tasks) == 2
    assert pipeline.tasks[1].name == "aggregate"
    assert pipeline.tasks[1].upstream_tasks == ["ingest"]


def test_parse_json_bundle():
    """Test parsing a JSON-formatted manifest."""
    json_data = """
    {
      "bundle": {
        "name": "json-bundle",
        "version": "2.0.0"
      },
      "resources": {
        "catalogs": [
          {"name": "json_catalog", "description": "From JSON"}
        ]
      }
    }
    """
    spec = parse_bundle_str(json_data, format="json")
    assert spec.bundle.name == "json-bundle"
    assert spec.bundle.version == "2.0.0"
    assert len(spec.resources.catalogs) == 1
    assert spec.resources.catalogs[0].name == "json_catalog"


def test_parse_bundle_file(tmp_path):
    """Test loading and parsing bundle files from disk."""
    toon_file = tmp_path / "lake.toon"
    toon_file.write_text(SAMPLE_TOON_BUNDLE, encoding="utf-8")

    spec_from_file = parse_bundle_file(toon_file)
    assert spec_from_file.bundle.name == "customer360-etl"

    yaml_file = tmp_path / "lake.yaml"
    yaml_file.write_text(SAMPLE_YAML_BUNDLE, encoding="utf-8")
    yaml_spec_from_file = parse_bundle_file(yaml_file)
    assert yaml_spec_from_file.bundle.name == "telemetry-lake"

    # Also test the shipped example bundle
    example_path = (
        Path(__file__).parent.parent / "examples" / "bundles" / "customer360" / "lake.toon"
    )
    if example_path.exists():
        example_spec = parse_bundle_file(example_path)
        assert example_spec.bundle.name == "customer360-medallion"
        assert len(example_spec.resources.schemas) == 3


def test_roundtrip_dump_and_reparse():
    """Test serializing to TOON, YAML, and JSON and reparsing."""
    spec = parse_bundle_str(SAMPLE_TOON_BUNDLE, format="toon")

    # TOON roundtrip
    toon_str = dump_bundle_to_str(spec, format="toon")
    reparsed_toon = parse_bundle_str(toon_str, format="toon")
    assert reparsed_toon.bundle.name == spec.bundle.name
    assert len(reparsed_toon.resources.catalogs) == len(spec.resources.catalogs)
    assert len(reparsed_toon.resources.tables) == len(spec.resources.tables)

    # YAML roundtrip
    yaml_str = dump_bundle_to_str(spec, format="yaml")
    reparsed_yaml = parse_bundle_str(yaml_str, format="yaml")
    assert reparsed_yaml.bundle.name == spec.bundle.name
    assert len(reparsed_yaml.resources.schemas) == len(spec.resources.schemas)

    # JSON roundtrip
    json_str = dump_bundle_to_str(spec, format="json")
    reparsed_json = parse_bundle_str(json_str, format="json")
    assert reparsed_json.bundle.name == spec.bundle.name


def test_variable_resolution_default():
    """Test variable substitution using default values."""
    spec = parse_bundle_str(SAMPLE_TOON_BUNDLE, format="toon")
    resolved = resolve_bundle(spec)

    # Default raw_bucket is 'datalake-bronze'
    assert (
        resolved.resources.tables[0].s3_path_pattern == "s3://datalake-bronze/customers/*.parquet"
    )
    assert (
        resolved.resources.tables[1].s3_path_pattern
        == "s3://datalake-bronze/silver/dim_customers/*.parquet"
    )


def test_variable_resolution_target_override():
    """Test variable substitution using target-specific overrides."""
    spec = parse_bundle_str(SAMPLE_TOON_BUNDLE, format="toon")

    # Target 'dev' overrides raw_bucket to 'datalake-dev'
    dev_resolved = resolve_bundle(spec, target="dev")
    assert (
        dev_resolved.resources.tables[0].s3_path_pattern == "s3://datalake-dev/customers/*.parquet"
    )

    # Target 'prod' overrides raw_bucket to 'datalake-prod'
    prod_resolved = resolve_bundle(spec, target="prod")
    assert (
        prod_resolved.resources.tables[0].s3_path_pattern
        == "s3://datalake-prod/customers/*.parquet"
    )


def test_variable_resolution_runtime_override():
    """Test explicit runtime variable overrides."""
    spec = parse_bundle_str(SAMPLE_TOON_BUNDLE, format="toon")
    override_resolved = resolve_bundle(
        spec, target="dev", variables_override={"raw_bucket": "custom-override-bucket"}
    )
    assert (
        override_resolved.resources.tables[0].s3_path_pattern
        == "s3://custom-override-bucket/customers/*.parquet"
    )


def test_variable_resolution_iterative_and_context():
    """Test contextual tokens and iterative variable substitution."""
    manifest = """
    bundle:
      name: my-app
      version: 3.1.0

    variables:
      region:
        default: us-east-1
      base_uri:
        default: "s3://bucket-${var.region}"
      full_path:
        default: "${var.base_uri}/${bundle.name}/${target.name}"

    resources:
      tables:
        items:
          s3_path_pattern: "${var.full_path}/data.parquet"
    """
    spec = parse_bundle_str(manifest, format="yaml")
    resolved = resolve_bundle(spec, target="staging")
    assert (
        resolved.resources.tables[0].s3_path_pattern
        == "s3://bucket-us-east-1/my-app/staging/data.parquet"
    )


def test_undefined_variable_raises():
    """Test that referencing an undefined variable raises BundleVariableError."""
    manifest = """
    bundle:
      name: bad-var
    resources:
      tables:
        bad_table:
          s3_path_pattern: "s3://${var.missing_variable}/test.parquet"
    """
    spec = parse_bundle_str(manifest, format="yaml")
    with pytest.raises(BundleVariableError, match="Undefined variable.*missing_variable"):
        resolve_bundle(spec)


def test_unknown_token_prefix_raises():
    """Test that referencing an unsupported token raises BundleVariableError."""
    manifest = """
    bundle:
      name: bad-token
    resources:
      tables:
        bad_table:
          s3_path_pattern: "s3://${unknown.prefix}/test.parquet"
    """
    spec = parse_bundle_str(manifest, format="yaml")
    with pytest.raises(BundleVariableError, match="Unknown interpolation token"):
        resolve_bundle(spec)


def test_circular_variable_raises():
    """Test that a circular reference between variables is detected and rejected."""
    manifest = """
    bundle:
      name: cycle-bundle
    variables:
      var_a:
        default: "value-${var.var_a}"
    resources:
      tables:
        t:
          s3_path_pattern: "${var_a}"
    """
    spec = parse_bundle_str(manifest, format="yaml")
    with pytest.raises(BundleVariableError, match="directly references itself"):
        resolve_bundle(spec)


def test_pipeline_dag_cycle_detection():
    """Test that cyclic DAG task dependencies raise BundleValidationError."""
    cyclic_manifest = """
    bundle:
      name: cyclic-pipeline
    resources:
      pipelines:
        loop_dag:
          tasks:
            task1:
              type: duckdb_sql
              entrypoint: sql/1.sql
              upstream_tasks: [task2]
            task2:
              type: duckdb_sql
              entrypoint: sql/2.sql
              upstream_tasks: [task1]
    """
    with pytest.raises(BundleValidationError, match="circular dependency cycle"):
        parse_bundle_str(cyclic_manifest, format="yaml")


def test_pipeline_task_self_dependency():
    """Test that a task depending on itself raises BundleValidationError."""
    self_dep_manifest = """
    bundle:
      name: self-dep
    resources:
      pipelines:
        p:
          tasks:
            task_a:
              type: duckdb_sql
              entrypoint: sql/a.sql
              upstream_tasks: [task_a]
    """
    with pytest.raises(BundleValidationError, match="cannot depend on itself"):
        parse_bundle_str(self_dep_manifest, format="yaml")


def test_pipeline_nonexistent_upstream_task():
    """Test that depending on a non-existent task raises BundleValidationError."""
    manifest = """
    bundle:
      name: missing-task
    resources:
      pipelines:
        p:
          tasks:
            task_a:
              type: duckdb_sql
              entrypoint: sql/a.sql
              upstream_tasks: [ghost_task]
    """
    with pytest.raises(BundleValidationError, match="non-existent upstream task 'ghost_task'"):
        parse_bundle_str(manifest, format="yaml")


def test_vector_index_invalid_metric():
    """Test that an unsupported distance metric raises BundleValidationError."""
    manifest = """
    bundle:
      name: bad-metric
    resources:
      vector_indexes:
        idx1:
          table: cat.sch.tbl
          column: embedding_col
          metric: manhattan_distance
    """
    with pytest.raises(BundleValidationError, match="unsupported metric 'manhattan_distance'"):
        parse_bundle_str(manifest, format="yaml")


def test_empty_manifest_raises():
    """Test that empty or whitespace manifest raises BundleParseError."""
    with pytest.raises(BundleParseError, match="Manifest content is empty"):
        parse_bundle_str("   ")


def test_malformed_json_raises():
    """Test that invalid JSON raises BundleParseError."""
    with pytest.raises(BundleParseError, match="Failed to parse JSON"):
        parse_bundle_str("{ invalid: json }", format="json")
