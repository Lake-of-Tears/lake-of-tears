"""Tests for Phase 2: PyIceberg ACID writes, storage_writer, LAB bundles and TOON metadata (#545)."""

from __future__ import annotations

from pathlib import Path

import iceberg_writer
import pyarrow as pa
from iceberg_testkit import _REPO_ROOT, client, iceberg_env  # noqa: F401
from models import CatalogTable

from pipeline.shared import iceberg_client, storage_writer

ARROW_V1 = pa.table({"id": pa.array([1, 2], pa.int64()), "city": ["Ardmore", "Henderson"]})
ARROW_V2 = pa.table(
    {
        "id": pa.array([3], pa.int64()),
        "city": ["Philadelphia"],
        "temp": pa.array([72.5], pa.float64()),
    }
)


def test_iceberg_writer_append_and_overwrite(env):
    """write_arrow in-process write helper supports atomic append and overwrite."""
    rest = env["rest"](env["writer"])
    rest.create_table(("main_lake", "bronze", "weather"), ARROW_V1.schema)

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="weather").one()

    # Append
    res1 = iceberg_writer.write_arrow(db, row, ARROW_V1, mode=iceberg_writer.MODE_APPEND)
    assert res1["mode"] == "append"
    assert res1["rows_written"] == 2
    reloaded = rest.load_table(("main_lake", "bronze", "weather"))
    assert reloaded.scan().to_arrow().num_rows == 2

    # Append with schema evolution (adds 'temp')
    res2 = iceberg_writer.write_arrow(db, row, ARROW_V2, mode=iceberg_writer.MODE_APPEND)
    assert res2["rows_written"] == 1
    reloaded = rest.load_table(("main_lake", "bronze", "weather"))
    assert reloaded.scan().to_arrow().num_rows == 3
    assert "temp" in reloaded.schema().column_names

    # Overwrite
    overwrite_data = pa.table(
        {"id": pa.array([10], pa.int64()), "city": ["Denver"], "temp": [55.0]}
    )
    res3 = iceberg_writer.write_arrow(db, row, overwrite_data, mode=iceberg_writer.MODE_OVERWRITE)
    assert res3["mode"] == "overwrite"
    assert res3["rows_written"] == 1
    reloaded = rest.load_table(("main_lake", "bronze", "weather"))
    assert reloaded.scan().to_arrow().num_rows == 1
    assert reloaded.scan().to_arrow()["city"].to_pylist() == ["Denver"]
    db.close()


def test_on_commit_toon_generation_and_caching(env):
    """Every commit generates Tier 1 table.toon and Tier 2 table_files.toon and caches them in DB."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "metrics"), ARROW_V1.schema)
    table.append(ARROW_V1)

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="metrics").one()
    assert row.iceberg is not None
    assert row.iceberg.toon_summary is not None
    assert "table: metrics" in row.iceberg.toon_summary
    assert "columns" in row.iceberg.toon_summary
    assert "id,BIGINT" in row.iceberg.toon_summary
    assert row.iceberg.toon_files is not None
    assert row.iceberg.toon_generated_at is not None

    # Verify table.toon persisted to storage
    metadata_dir = Path(row.iceberg.location.removeprefix("file://")) / "metadata"
    assert (metadata_dir / "table.toon").exists()
    assert (metadata_dir / "table_files.toon").exists()
    assert (metadata_dir / "version-hint.text").exists()

    # Verify export endpoint with tiers
    token = env["token"](env["writer"])
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-Slug": "dev"}
    cat_id = str(env["cat"].id)
    sch_id = str(row.schema_id)
    tbl_id = str(row.id)

    # Tier 1
    resp_t1 = client.get(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/toon?tier=1&format=raw",
        headers=headers,
    )
    assert resp_t1.status_code == 200
    assert "table: metrics" in resp_t1.text
    assert "rows: 2" in resp_t1.text

    # Tier 2
    resp_t2 = client.get(
        f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/toon?tier=2&format=raw",
        headers=headers,
    )
    assert resp_t2.status_code == 200
    assert "files:" in resp_t2.text or "table: metrics" in resp_t2.text
    db.close()


def test_storage_writer_iceberg_integration(env, monkeypatch):
    """storage_writer.write_to_storage routes format='iceberg' through the native Iceberg REST client."""
    token = env["token"](env["writer"])
    monkeypatch.setenv("LOT_BACKEND_URL", "http://lot.test")
    monkeypatch.setenv("WORKSPACE_SLUG", "dev")
    monkeypatch.setenv("LOT_TASK_TOKEN", token)

    # Wire iceberg_client's catalog loader to use the in-process test catalog
    def _mock_get_catalog(workspace=None, **properties):
        return env["rest"](env["writer"], warehouse=workspace or "dev")

    monkeypatch.setattr(iceberg_client, "get_catalog", _mock_get_catalog)

    import pandas as pd

    df = pd.DataFrame({"id": [101, 102], "city": ["Denver", "Austin"]})
    loc = storage_writer.write_to_storage(
        df,
        workspace="dev",
        catalog="main_lake",
        schema="bronze",
        table="sensors",
        format="iceberg",
        mode="append",
    )
    assert loc.startswith("file://")

    # Read back through REST and DuckDB
    rest = env["rest"](env["writer"])
    loaded = rest.load_table(("main_lake", "bronze", "sensors"))
    assert loaded.scan().to_arrow().num_rows == 2

    # Append second batch with pandas DataFrame
    df2 = pd.DataFrame({"id": [103], "city": ["Boulder"]})
    storage_writer.write_to_storage(
        df2,
        workspace="dev",
        catalog="main_lake",
        schema="bronze",
        table="sensors",
        format="iceberg",
        mode="append",
    )
    assert loaded.refresh().scan().to_arrow().num_rows == 3


def test_lab_bundle_iceberg_table_materialization(env):
    """BundleDeployer materializes declared Iceberg tables with partition specs."""
    from models import CatalogSchema

    from backend.bundles.deployer import BundleDeployer
    from backend.bundles.schemas import BundleMetadata, BundleSpec, ResourceInventory, TableResource

    db = env["Session"]()
    ws = db.merge(env["ws"])
    schema_row = db.query(CatalogSchema).filter_by(catalog_id=env["cat"].id, slug="bronze").one()

    tbl_resource = TableResource(
        name="telemetry",
        catalog="main_lake",
        schema="bronze",
        format="iceberg",
        partition_by=["region"],
        columns=[
            {"name": "id", "type": "BIGINT"},
            {"name": "region", "type": "VARCHAR"},
            {"name": "latency", "type": "DOUBLE"},
        ],
    )
    bundle_spec = BundleSpec(
        bundle=BundleMetadata(name="iot-telemetry"),
        resources=ResourceInventory(tables=[tbl_resource]),
    )
    assert bundle_spec.bundle.name == "iot-telemetry"

    tbl = CatalogTable(
        schema_id=schema_row.id,
        name=tbl_resource.name,
        slug=tbl_resource.name.lower(),
        format="iceberg",
        column_defs=[c.model_dump() for c in tbl_resource.columns],
    )
    db.add(tbl)
    db.commit()

    BundleDeployer._materialize_iceberg_table(db, ws, tbl, ["region"])
    db.refresh(tbl)

    assert tbl.format == "iceberg"
    assert tbl.iceberg is not None
    assert tbl.iceberg.metadata_location.endswith(".metadata.json")

    rest = env["rest"](env["writer"])
    ice_table = rest.load_table(("main_lake", "bronze", "telemetry"))
    assert ice_table.spec().fields[0].name == "region"
    db.close()
