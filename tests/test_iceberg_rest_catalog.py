"""Native Iceberg REST catalog, pointer table, auto-detection and unified read helper (#544)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import iceberg_catalog  # noqa: E402  (backend/ is put on sys.path by the testkit)
import pyarrow as pa
import pytest
import table_scan  # noqa: E402
from iceberg_testkit import _REPO_ROOT, client, iceberg_env  # noqa: F401
from models import CatalogSchema, CatalogTable, DeletedCatalogAsset, IcebergTable  # noqa: E402
from pyiceberg.exceptions import (
    CommitFailedException,
    ForbiddenError,
    NamespaceAlreadyExistsError,
    NoSuchNamespaceError,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.table.update import SetLocationUpdate, SetPropertiesUpdate
from pyiceberg.types import LongType

ARROW = pa.table({"id": pa.array([1, 2], pa.int64()), "name": ["a", "b"]})


def _local(path: str) -> str:
    return path.removeprefix("file://")


def test_config_resolves_workspace_prefix(env):
    token = env["token"](env["writer"])
    resp = client.get(
        "/api/v1/iceberg/v1/config?warehouse=dev", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["overrides"]["prefix"] == "dev"
    assert "secret" not in resp.text.lower()

    assert client.get("/api/v1/iceberg/v1/config?warehouse=dev").status_code == 401


def test_namespaces_map_to_catalogs_and_schemas(env):
    rest = env["rest"](env["writer"])
    assert rest.list_namespaces() == [("main_lake",)]
    assert rest.list_namespaces("main_lake") == [("main_lake", "bronze")]

    rest.create_namespace(("main_lake", "staging"), {"comment": "scratch"})
    assert ("main_lake", "staging") in rest.list_namespaces("main_lake")
    assert rest.load_namespace_properties(("main_lake", "staging"))["comment"] == "scratch"
    with pytest.raises(NamespaceAlreadyExistsError):
        rest.create_namespace(("main_lake", "staging"))

    rest.drop_namespace(("main_lake", "staging"))
    assert ("main_lake", "staging") not in rest.list_namespaces("main_lake")
    with pytest.raises(NoSuchNamespaceError):
        rest.load_namespace_properties(("main_lake", "nope"))
    # Core medallion schemas are locked, matching the catalog UI.
    with pytest.raises(ForbiddenError):
        rest.drop_namespace(("main_lake", "bronze"))


def test_create_append_and_read_through_rest_and_duckdb(env):
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "events"), ARROW.schema)
    assert table.metadata.location == f"file://{env['root']}/workspaces/dev/main_lake/bronze/events"

    table.append(ARROW)
    first_snapshot = table.current_snapshot().snapshot_id
    table.append(pa.table({"id": pa.array([3], pa.int64()), "name": ["c"]}))

    reloaded = rest.load_table("main_lake.bronze.events")
    assert reloaded.scan().to_arrow().num_rows == 3
    assert rest.list_tables(("main_lake", "bronze")) == [("main_lake", "bronze", "events")]

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="events").one()
    assert row.format == "iceberg"
    assert row.iceberg.metadata_location == reloaded.metadata_location
    assert row.iceberg.previous_metadata_location
    assert row.iceberg.current_snapshot_id == reloaded.current_snapshot().snapshot_id
    assert [(c["name"], c["type"]) for c in row.column_defs] == [
        ("id", "BIGINT"),
        ("name", "VARCHAR"),
    ]

    # The unified read helper routes this row to iceberg_scan pinned at the committed metadata.
    scan = table_scan.table_scan_for(row)
    assert scan.startswith("iceberg_scan(")
    con = duckdb.connect()
    assert table_scan.ensure_iceberg_loaded(con)
    assert con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0] == 3
    pinned = table_scan.table_scan_for(row, snapshot_id=first_snapshot)
    assert con.execute(f"SELECT count(*) FROM {pinned}").fetchone()[0] == 2
    db.close()


def test_schema_evolution_syncs_column_defs(env):
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "evolve"), ARROW.schema)
    wider = pa.table({"id": pa.array([9], pa.int64()), "name": ["z"], "score": [1.5]})
    with table.update_schema() as update:
        update.union_by_name(wider.schema)
    table.append(wider)

    db = env["Session"]()
    row = db.query(CatalogTable).filter_by(slug="evolve").one()
    assert [c["name"] for c in row.column_defs] == ["id", "name", "score"]
    assert row.iceberg.current_schema_id == 1
    db.close()


def test_staged_create_transaction(env):
    rest = env["rest"](env["writer"])
    with rest.create_table_transaction(("main_lake", "bronze", "staged"), ARROW.schema) as txn:
        txn.append(ARROW)
    assert rest.load_table(("main_lake", "bronze", "staged")).scan().to_arrow().num_rows == 2


def test_concurrent_commit_conflict_is_rejected(env):
    rest = env["rest"](env["writer"])
    rest.create_table(("main_lake", "bronze", "race"), ARROW.schema)
    a = rest.load_table(("main_lake", "bronze", "race"))
    b = rest.load_table(("main_lake", "bronze", "race"))
    with a.update_schema() as update:
        update.add_column("x", LongType())
    # `b` still asserts the old schema id / last-assigned field id.
    with pytest.raises(CommitFailedException):
        with b.update_schema() as update:
            update.add_column("y", LongType())


def test_pointer_swap_is_optimistic(env):
    """A stale pointer (another process committed in between) must fail the swap, not clobber."""
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "swap"), ARROW.schema)
    db = env["Session"]()
    cat = iceberg_catalog.LotIcebergCatalog(db, db.merge(env["ws"]), enforce_acl=False)
    original_to_table = cat._to_table

    def _stale(*args):
        loaded = original_to_table(*args)
        db.query(IcebergTable).update({"metadata_location": "file:///elsewhere.metadata.json"})
        db.commit()
        return loaded

    cat._to_table = _stale
    with pytest.raises(CommitFailedException):
        cat.commit_identifier(table.name(), (), (SetPropertiesUpdate(updates={"k": "v"}),))
    db.close()


def test_rbac_read_only_and_cross_workspace(env):
    env["rest"](env["writer"]).create_table(("main_lake", "bronze", "secure"), ARROW.schema)

    reader = env["rest"](env["reader"])
    assert reader.load_table(("main_lake", "bronze", "secure")).schema().column_names == [
        "id",
        "name",
    ]
    with pytest.raises(ForbiddenError):
        reader.create_table(("main_lake", "bronze", "nope"), ARROW.schema)
    with pytest.raises(ForbiddenError):
        reader.load_table(("main_lake", "bronze", "secure")).append(ARROW)
    with pytest.raises(ForbiddenError):
        reader.drop_table(("main_lake", "bronze", "secure"))

    # A user from another workspace cannot even open this workspace's prefix.
    with pytest.raises(Exception) as denied:
        env["rest"](env["outsider"])
    assert "403" in str(denied.value) or "member" in str(denied.value).lower()

    # Within their own workspace the other workspace's catalogs are invisible.
    theirs = env["rest"](env["outsider"], warehouse="other")
    assert theirs.list_namespaces() == [("foreign",)]
    with pytest.raises(NoSuchTableError):
        theirs.load_table(("main_lake", "bronze", "secure"))


def test_location_is_jailed_to_workspace(env):
    rest = env["rest"](env["writer"])
    with pytest.raises(ForbiddenError):
        rest.create_table(
            ("main_lake", "bronze", "escape"),
            ARROW.schema,
            location=f"file://{env['root']}/workspaces/other/main_lake/bronze/escape",
        )
    with pytest.raises(ForbiddenError):
        rest.create_table(
            ("main_lake", "bronze", "escape"),
            ARROW.schema,
            location=f"file://{env['root']}/workspaces/dev/../other/x",
        )
    table = rest.create_table(("main_lake", "bronze", "mover"), ARROW.schema)
    with pytest.raises(ForbiddenError):
        with table.transaction() as txn:
            txn._apply(
                (SetLocationUpdate(location=f"file://{env['root']}/workspaces/other/steal"),)
            )


def test_duplicate_table_and_register(env):
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "dup"), ARROW.schema)
    with pytest.raises(TableAlreadyExistsError):
        rest.create_table(("main_lake", "bronze", "dup"), ARROW.schema)
    table.append(ARROW)
    metadata_location = rest.load_table(("main_lake", "bronze", "dup")).metadata_location

    registered = rest.register_table(("main_lake", "bronze", "dup_copy"), metadata_location)
    assert registered.scan().to_arrow().num_rows == 2


def test_drop_goes_to_trash_and_restores_pointer(env):
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "trashme"), ARROW.schema)
    table.append(ARROW)
    metadata_location = rest.load_table(("main_lake", "bronze", "trashme")).metadata_location

    rest.drop_table(("main_lake", "bronze", "trashme"))
    assert not rest.table_exists(("main_lake", "bronze", "trashme"))

    db = env["Session"]()
    asset = db.query(DeletedCatalogAsset).filter_by(slug="trashme").one()
    assert asset.format == "iceberg"
    assert asset.trash_s3_prefix is None, "Iceberg files must stay in place while in Trash"
    assert Path(_local(metadata_location)).exists()

    import catalog_trash_service

    schema = db.query(CatalogSchema).filter_by(id=asset.schema_id).one()
    catalog_trash_service._restore_table_record(db, asset, schema, db.merge(env["ws"]))
    db.commit()
    db.close()
    assert rest.load_table(("main_lake", "bronze", "trashme")).scan().to_arrow().num_rows == 2


def test_auto_detection_finds_latest_metadata(env, tmp_path):
    rest = env["rest"](env["writer"])
    table = rest.create_table(("main_lake", "bronze", "detect"), ARROW.schema)
    table.append(ARROW)
    current = rest.load_table(("main_lake", "bronze", "detect")).metadata_location
    location = table.metadata.location

    assert iceberg_catalog.detect_iceberg_metadata(location) == current
    assert iceberg_catalog.detect_iceberg_metadata(f"{location}/**/*.parquet") == current
    assert iceberg_catalog.detect_iceberg_metadata(f"{location}/data/") == current

    plain = tmp_path / "plain"
    plain.mkdir()
    assert iceberg_catalog.detect_iceberg_metadata(f"file://{plain}/**/*.parquet") is None

    # Hadoop-style version files are recognised too, highest version wins.
    hadoop = tmp_path / "hadoop" / "metadata"
    hadoop.mkdir(parents=True)
    for name in ("v1.metadata.json", "v12.metadata.json", "v3.metadata.json", "version-hint.text"):
        (hadoop / name).write_text("{}")
    assert iceberg_catalog.detect_iceberg_metadata(f"file://{tmp_path}/hadoop").endswith(
        "/metadata/v12.metadata.json"
    )


def test_table_read_scan_routing():
    assert (
        table_scan.table_read_scan("s3://datalake/raw/x/**/*.parquet")
        == "read_parquet('s3://datalake/raw/x/**/*.parquet')"
    )
    assert table_scan.table_read_scan("s3://b/it's/*.parquet", union_by_name=True) == (
        "read_parquet('s3://b/it''s/*.parquet', union_by_name = true)"
    )
    iceberg = {
        "format": "iceberg",
        "s3_path_pattern": "s3://b/t/",
        "iceberg_metadata_location": "s3://b/t/metadata/00002-x.metadata.json",
    }
    assert table_scan.table_scan_for(iceberg) == (
        "iceberg_scan('s3://b/t/metadata/00002-x.metadata.json')"
    )
    assert "snapshot_from_id = 42" in table_scan.table_scan_for(iceberg, snapshot_id="42")
    assert "snapshot_from_timestamp = CAST('2026-01-01 00:00:00' AS TIMESTAMP)" in (
        table_scan.table_scan_for(iceberg, as_of_timestamp="2026-01-01 00:00:00")
    )
    with pytest.raises(ValueError):
        table_scan.table_scan_for(iceberg, snapshot_id="1; DROP TABLE x")
    with pytest.raises(ValueError):
        table_scan.table_read_scan("s3://b/p/*.parquet", snapshot_id=1)
    with pytest.raises(ValueError):
        table_scan.table_read_scan(None, table_format="iceberg")
    assert table_scan.table_format_of({"format": None}) == "parquet"


def test_table_scan_copies_are_identical():
    """Each container ships only its own directory, so the helper is vendored three times."""
    source = (_REPO_ROOT / "backend" / "table_scan.py").read_text()
    assert (_REPO_ROOT / "ui" / "table_scan.py").read_text() == source
    assert (_REPO_ROOT / "pipeline" / "shared" / "table_scan.py").read_text() == source
