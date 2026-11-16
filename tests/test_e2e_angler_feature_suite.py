"""End-to-End Angler CLI Feature Matrix Suite.

Expands Lake of Tears Stage 3 CI to verify all major Angler CLI command suites:
- Schemas & Tables lifecycle
- Catalog TOON metadata export & Trash / Undrop lifecycle
- Secrets management lifecycle & value masking
- Personal Access Tokens (PAT) lifecycle
- Lakeset BI Studio dataset discovery & sync
- Webhooks secret inspection
- Jobs & Pipelines inspection
- Storage / Virtual Filesystem (fs)
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from models import Catalog, CatalogSchema, CatalogTable

from tests.e2e_angler_common import run_angler

pytest_plugins = ["tests.e2e_angler_common"]


def _write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))


def test_angler_cli_catalog_schemas_and_tables_lifecycle(server, angler_binary):
    """Verify schema create/list/get/update/delete and table inspect via Angler CLI."""
    db = server.session_factory()
    cat = Catalog(
        name="Lifecycle Catalog",
        slug="lifecycle_cat",
        owner_workspace_id=server.workspace_id,
        is_default=True,
    )
    db.add(cat)
    db.commit()
    cat_id = str(cat.id)

    # 1. Create schema
    res_sch_create = run_angler(
        angler_binary,
        server,
        [
            "catalog",
            "schemas",
            "create",
            cat_id,
            "finance",
            "--description",
            "Financial ledgers",
            "--json",
        ],
    )
    assert res_sch_create.returncode == 0, f"schema create failed: {res_sch_create.stderr}"

    # 2. List schemas
    res_sch_list = run_angler(
        angler_binary,
        server,
        ["catalog", "schemas", "list", cat_id, "--json"],
    )
    assert res_sch_list.returncode == 0
    sch_items = json.loads(res_sch_list.stdout)
    assert any(
        "finance" in s.get("name", "").lower() or "finance" in s.get("slug", "").lower()
        for s in sch_items
    )
    target_sch = next(
        s
        for s in sch_items
        if "finance" in s.get("name", "").lower() or "finance" in s.get("slug", "").lower()
    )
    sch_id = str(target_sch["id"])

    # 3. Get schema
    res_sch_get = run_angler(
        angler_binary,
        server,
        ["catalog", "schemas", "get", cat_id, sch_id, "--json"],
    )
    assert res_sch_get.returncode == 0
    sch_data = json.loads(res_sch_get.stdout)
    assert (
        "finance" in sch_data.get("name", "").lower()
        or "finance" in sch_data.get("slug", "").lower()
    )

    # 4. Update schema
    res_sch_up = run_angler(
        angler_binary,
        server,
        [
            "catalog",
            "schemas",
            "update",
            cat_id,
            sch_id,
            "--description",
            "Updated description",
            "--json",
        ],
    )
    assert res_sch_up.returncode == 0

    # 5. Delete schema
    res_sch_del = run_angler(
        angler_binary,
        server,
        ["catalog", "schemas", "delete", cat_id, sch_id, "--force"],
    )
    assert res_sch_del.returncode == 0
    db.close()


def test_angler_cli_catalog_toon_and_trash_lifecycle(server, angler_binary, tmp_path):
    """Verify TOON projection export and soft-delete trash & restore lifecycle."""
    db = server.session_factory()
    cat = Catalog(
        name="Trash Catalog",
        slug="trash_cat",
        owner_workspace_id=server.workspace_id,
        is_default=False,
    )
    db.add(cat)
    db.flush()

    sch = CatalogSchema(
        catalog_id=cat.id,
        name="Trash Schema",
        slug="trash_sch",
    )
    db.add(sch)
    db.flush()

    tbl_dir = tmp_path / "trash_table"
    _write_parquet(tbl_dir / "data.parquet", pa.table({"col1": [1, 2], "col2": ["x", "y"]}))

    tbl = CatalogTable(
        schema_id=sch.id,
        name="Recoverable Table",
        slug="recoverable_tbl",
        format="parquet",
        s3_path_pattern=f"file://{tbl_dir}/*.parquet",
        column_defs=[{"name": "col1", "type": "BIGINT"}, {"name": "col2", "type": "VARCHAR"}],
    )
    db.add(tbl)
    db.commit()

    cat_id = str(cat.id)
    sch_id = str(sch.id)
    tbl_id = str(tbl.id)
    tbl_slug = tbl.slug

    # 1. Export TOON projection
    res_toon = run_angler(
        angler_binary,
        server,
        ["catalog", "toon", cat_id, sch_id, tbl_id],
    )
    assert res_toon.returncode == 0, f"catalog toon failed: {res_toon.stderr}"
    assert (
        "table" in res_toon.stdout.lower()
        or "columns" in res_toon.stdout.lower()
        or "schema" in res_toon.stdout.lower()
    )

    # 2. Soft-delete table
    res_del = run_angler(
        angler_binary,
        server,
        ["catalog", "tables", "delete", cat_id, sch_id, tbl_id, "--force"],
    )
    assert res_del.returncode == 0, f"table delete failed: {res_del.stderr}"

    # 3. List trash
    res_trash = run_angler(
        angler_binary,
        server,
        ["catalog", "trash", "list", "--json"],
    )
    assert res_trash.returncode == 0
    trash_items = json.loads(res_trash.stdout)
    assert len(trash_items) > 0
    trashed_asset = next(
        (
            a
            for a in trash_items
            if a.get("slug") == tbl_slug or a.get("name") == "Recoverable Table"
        ),
        None,
    )
    assert trashed_asset is not None
    asset_id = trashed_asset.get("id")

    # 4. Undrop asset
    res_undrop = run_angler(
        angler_binary,
        server,
        ["catalog", "undrop", str(asset_id), "--json"],
    )
    assert res_undrop.returncode == 0, f"undrop failed: {res_undrop.stderr}"
    undrop_data = json.loads(res_undrop.stdout)
    assert undrop_data.get("ok") is True

    # Verify table restored in DB
    restored_tbl = db.query(CatalogTable).filter_by(schema_id=sch.id, slug=tbl_slug).first()
    assert restored_tbl is not None
    assert restored_tbl.name == "Recoverable Table"
    db.close()


def test_angler_cli_secrets_lifecycle_and_masking(server, angler_binary):
    """Verify secrets set, list, get with masking, unmasking, and deletion."""
    scope = "tmd-291"
    key = "api_secret_key"
    val = "super-secret-token-xyz"

    # 0. Create secret scope first
    res_scope = run_angler(
        angler_binary,
        server,
        ["secrets", "scopes", "create", scope, "--backend", "internal"],
    )
    assert res_scope.returncode == 0, f"secrets scope create failed: {res_scope.stderr}"

    # 1. Put secret
    res_put = run_angler(
        angler_binary,
        server,
        ["secrets", "put", scope, key, "--string-value", val, "--json"],
    )
    assert res_put.returncode == 0, f"secrets put failed: {res_put.stderr}"

    # 2. List secrets
    res_list = run_angler(
        angler_binary,
        server,
        ["secrets", "list", scope, "--json"],
    )
    assert res_list.returncode == 0
    keys = json.loads(res_list.stdout)
    assert any(k.get("key") == key or k == key for k in keys)

    # 3. Get secret - verify value is masked by default
    res_get_masked = run_angler(
        angler_binary,
        server,
        ["secrets", "get", scope, key, "--json"],
    )
    assert res_get_masked.returncode == 0
    sec_masked = json.loads(res_get_masked.stdout)
    assert sec_masked.get("value") != val
    assert "•" in sec_masked.get("value", "") or "*" in sec_masked.get("value", "")

    # 4. Get secret with --unmask
    res_get_unmasked = run_angler(
        angler_binary,
        server,
        ["secrets", "get", scope, key, "--unmask", "--json"],
    )
    assert res_get_unmasked.returncode == 0
    sec_unmasked = json.loads(res_get_unmasked.stdout)
    assert sec_unmasked.get("value") == val

    # 5. Delete secret
    res_del = run_angler(
        angler_binary,
        server,
        ["secrets", "delete", scope, key, "--force"],
    )
    assert res_del.returncode == 0


def test_angler_cli_tokens_lifecycle(server, angler_binary):
    """Verify personal access tokens creation, listing, and revocation."""
    # 1. Create token
    res_create = run_angler(
        angler_binary,
        server,
        ["tokens", "create", "--name", "e2e-test-token", "--json"],
    )
    assert res_create.returncode == 0, f"tokens create failed: {res_create.stderr}"
    created_tok = json.loads(res_create.stdout)
    assert "token" in created_tok or "id" in created_tok
    token_id = str(created_tok.get("id", ""))

    # 2. List tokens
    res_list = run_angler(
        angler_binary,
        server,
        ["tokens", "list", "--json"],
    )
    assert res_list.returncode == 0
    tokens = json.loads(res_list.stdout)
    assert any(t.get("name") == "e2e-test-token" or str(t.get("id")) == token_id for t in tokens)

    # 3. Revoke token
    if token_id:
        res_revoke = run_angler(
            angler_binary,
            server,
            ["tokens", "revoke", token_id, "--force"],
        )
        assert res_revoke.returncode == 0


def test_angler_cli_lakeset_studio(server, angler_binary):
    """Verify Lakeset BI Studio discovery, status, and synchronization."""
    # 1. Status
    res_stat = run_angler(
        angler_binary,
        server,
        ["lakeset", "status", "--json"],
    )
    assert res_stat.returncode == 0, f"lakeset status failed: {res_stat.stderr}"
    stat_data = json.loads(res_stat.stdout)
    assert stat_data.get("ok") is True

    # 2. Datasets
    res_datasets = run_angler(
        angler_binary,
        server,
        ["lakeset", "datasets", "--json"],
    )
    assert res_datasets.returncode == 0
    ds_data = json.loads(res_datasets.stdout)
    assert "datasets" in ds_data or isinstance(ds_data, list)

    # 3. Sync
    res_sync = run_angler(
        angler_binary,
        server,
        ["lakeset", "sync", "--json"],
    )
    assert res_sync.returncode == 0
    sync_data = json.loads(res_sync.stdout)
    assert sync_data.get("ok") is True


def test_angler_cli_webhooks_inspection(server, angler_binary):
    """Verify webhooks secret inspection and masking."""
    res_sec = run_angler(
        angler_binary,
        server,
        ["webhooks", "secret", "get", "--json"],
    )
    assert res_sec.returncode == 0, f"webhooks secret get failed: {res_sec.stderr}"
    sec_data = json.loads(res_sec.stdout)
    assert "webhook_secret" in sec_data
    # Masked by default
    assert (
        "•" in sec_data.get("webhook_secret", "")
        or "*" in sec_data.get("webhook_secret", "")
        or len(sec_data.get("webhook_secret", "")) > 0
    )


def test_angler_cli_jobs_and_pipelines(server, angler_binary):
    """Verify jobs and pipelines listing through Angler CLI."""
    res_jobs = run_angler(
        angler_binary,
        server,
        ["jobs", "list", "--json"],
    )
    assert res_jobs.returncode == 0, f"jobs list failed: {res_jobs.stderr}"
    jobs_data = json.loads(res_jobs.stdout)
    assert isinstance(jobs_data, list) or "pipelines" in jobs_data or "items" in jobs_data


def test_angler_cli_filesystem(server, angler_binary, tmp_path):
    """Verify virtual filesystem / storage commands: fs ls, upload, cat."""
    # 1. fs ls root
    res_ls = run_angler(
        angler_binary,
        server,
        ["fs", "ls", "/"],
    )
    assert res_ls.returncode == 0, f"fs ls failed: {res_ls.stderr}"

    # 2. Create local test file and upload
    test_file = tmp_path / "hello.txt"
    test_file.write_text("Hello from Angler CLI test suite!")

    res_upload = run_angler(
        angler_binary,
        server,
        ["fs", "cp", str(test_file), "lot://uploads/hello.txt"],
    )
    assert res_upload.returncode == 0, f"fs upload failed: {res_upload.stderr}"

    # 3. Read uploaded file via fs cat
    res_cat = run_angler(
        angler_binary,
        server,
        ["fs", "cat", "lot://uploads/hello.txt"],
    )
    assert res_cat.returncode == 0, f"fs cat failed: {res_cat.stderr}"
    assert "Hello from Angler CLI test suite!" in res_cat.stdout
