"""LoT-native API for Iceberg catalog tables: snapshot history, RESTORE, TOON metadata views,
maintenance (OPTIMIZE / VACUUM) and Parquet-to-Iceberg migration.

The standard Iceberg REST catalog protocol lives in `iceberg_rest_router`; these endpoints
power the Catalog UI, the `lot` CLI and the nightly maintenance DAG.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Any

import iceberg_maintenance
import iceberg_migration
import iceberg_service
import iceberg_toon
from auth import SECRET_KEY, get_catalog_workspace_and_role, get_current_user
from catalog_acl import caller_access
from database import get_db
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from models import Catalog, CatalogSchema, CatalogTable, User, Workspace
from pydantic import BaseModel
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.iceberg.tables")

router = APIRouter()
internal_router = APIRouter()

_TABLE = "/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}"


class RestoreRequest(BaseModel):
    snapshot_id: str | int | None = None
    as_of: str | None = None


class OptimizeRequest(BaseModel):
    dry_run: bool = False
    compaction_min_files: int | None = None
    target_file_size_mb: int | None = None
    max_rewrite_mb: int | None = None


class VacuumRequest(BaseModel):
    dry_run: bool = False
    snapshot_retention_days: int | None = None
    retain_last_snapshots: int | None = None
    orphan_older_than_hours: int | None = None


class MaintenancePolicyRequest(BaseModel):
    enabled: bool | None = None
    compaction_enabled: bool | None = None
    compaction_min_files: int | None = None
    target_file_size_mb: int | None = None
    max_rewrite_mb: int | None = None
    snapshot_retention_days: int | None = None
    retain_last_snapshots: int | None = None
    orphan_older_than_hours: int | None = None


class MaintenanceRunRequest(BaseModel):
    dry_run: bool = False
    force: bool = False


class MigrateRequest(BaseModel):
    table: str | None = None
    dry_run: bool = False
    verify: bool = True


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _resolve_catalog(
    db: Session,
    user: User,
    ws_ctx: tuple[Workspace, str] | None,
    catalog_id: str,
    require: str,
) -> Catalog:
    ws = ws_ctx[0] if isinstance(ws_ctx, tuple) and ws_ctx and ws_ctx[0] is not None else None
    cat_uuid = _uuid(catalog_id)
    query = db.query(Catalog).filter(Catalog.deleted_at.is_(None))
    catalog = (
        query.filter(Catalog.id == cat_uuid).first()
        if cat_uuid
        else query.filter(Catalog.slug == str(catalog_id)).first()
    )
    access = caller_access(db, catalog, user, ws=ws) if catalog else None
    if catalog is None or access is None:
        # Strict invisibility outside the active workspace / approved shares
        raise HTTPException(404, "Catalog not found")
    if isinstance(ws_ctx, tuple) and len(ws_ctx) == 2 and ws_ctx[1] in ("read", "viewer", "task"):
        if access in ("owner", "write"):
            access = "read"  # token / role capped below the membership's catalog access
    if require == "write" and access not in ("owner", "write"):
        raise HTTPException(403, "Write access required")
    if require == "owner" and access != "owner":
        raise HTTPException(403, "Catalog owner access required")
    return catalog


def _resolve_table(
    db: Session,
    user: User,
    ws_ctx: tuple[Workspace, str] | None,
    catalog_id: str,
    schema_id: str,
    table_id: str,
    require: str = "read",
    iceberg: bool = True,
) -> CatalogTable:
    catalog = _resolve_catalog(db, user, ws_ctx, catalog_id, require)
    sch_uuid = _uuid(schema_id)
    schema = (
        db.query(CatalogSchema).filter_by(id=sch_uuid, catalog_id=catalog.id).first()
        if sch_uuid
        else db.query(CatalogSchema).filter_by(slug=str(schema_id), catalog_id=catalog.id).first()
    )
    if not schema:
        raise HTTPException(404, "Schema not found")
    tbl_uuid = _uuid(table_id)
    table = (
        db.query(CatalogTable).filter_by(id=tbl_uuid, schema_id=schema.id).first()
        if tbl_uuid
        else db.query(CatalogTable).filter_by(slug=str(table_id), schema_id=schema.id).first()
    )
    if not table:
        raise HTTPException(404, "Table not found")
    if iceberg and (table.format != "iceberg" or not table.iceberg):
        raise HTTPException(400, "This operation is only available for Iceberg tables")
    return table


def _run(operation: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("Iceberg table operation failed")
        raise HTTPException(500, f"Iceberg operation failed: {exc}") from exc


def _overrides(req: BaseModel) -> dict[str, Any]:
    return {k: v for k, v in req.model_dump().items() if k not in ("dry_run", "force")}


# ── Details, snapshot history & RESTORE ───────────────────────────────────────


@router.get(_TABLE + "/iceberg")
def get_iceberg_details(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id)
    ice = table.iceberg
    state = ice.maintenance or {}
    return {
        "table_id": str(table.id),
        "table": table.slug,
        "format": table.format,
        "table_uuid": ice.table_uuid,
        "location": ice.location,
        "metadata_location": ice.metadata_location,
        "current_schema_id": ice.current_schema_id,
        "current_snapshot_id": (
            str(ice.current_snapshot_id) if ice.current_snapshot_id is not None else None
        ),
        "toon_generated_at": ice.toon_generated_at.isoformat() if ice.toon_generated_at else None,
        "maintenance_policy": iceberg_maintenance.get_policy(table),
        "last_maintenance": state.get("last_run"),
        "migrated_from": state.get("migrated_from"),
        "updated_at": ice.updated_at.isoformat() if ice.updated_at else None,
    }


@router.get(_TABLE + "/iceberg/snapshots")
@router.get(_TABLE + "/snapshots")
def get_snapshots(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id)
    return _run(iceberg_service.list_snapshots, db, table)


@router.post(_TABLE + "/iceberg/restore")
@router.post(_TABLE + "/restore")
def restore_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: RestoreRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    result = _run(
        iceberg_service.restore_table, db, table, snapshot_id=req.snapshot_id, as_of=req.as_of
    )
    logger.info("User %s restored table %s: %s", user.email, table.slug, result)
    return {"ok": True, **result}


# ── TOON metadata views ───────────────────────────────────────────────────────


@router.get(_TABLE + "/iceberg/toon")
def get_table_toon(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    tier: int = Query(1, ge=1, le=2),
    partition: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    format: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    """Tier 1 `table.toon` (summary) or Tier 2 `table_files.toon` (file manifest).

    Tier 2 accepts `partition` (substring of the partition path, e.g. `year=2026`), `limit`
    and `offset` for a parameterized slice read live from the manifests.
    """
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id)
    if tier == 2 and (partition or limit or offset):
        text = _run(
            iceberg_toon.slice_files_toon,
            db,
            table,
            partition=partition,
            limit=limit,
            offset=offset,
        )
    else:
        text = _run(iceberg_toon.get_table_toon, db, table, tier)
    if (format or "").strip().lower() in ("raw", "toon", "text"):
        return PlainTextResponse(text, media_type="text/plain")
    name = iceberg_toon.TIER1_FILE if tier == 1 else iceberg_toon.TIER2_FILE
    ice = table.iceberg
    return {
        "ok": True,
        "tier": tier,
        "toon": text,
        "table": table.slug,
        "filename": name,
        "snapshot_id": (
            str(ice.current_snapshot_id) if ice.current_snapshot_id is not None else None
        ),
        "generated_at": ice.toon_generated_at.isoformat() if ice.toon_generated_at else None,
        "approx_tokens": max(1, len(text) // 4),
    }


@router.post(_TABLE + "/iceberg/toon/refresh")
def refresh_table_toon(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    tier1, tier2 = _run(iceberg_toon.refresh_table_toon, db, table)
    return {"ok": True, "tier1_chars": len(tier1), "tier2_chars": len(tier2)}


# ── Maintenance: OPTIMIZE / VACUUM / policy ───────────────────────────────────


@router.post(_TABLE + "/iceberg/optimize")
def optimize_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: OptimizeRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    """OPTIMIZE: bin-pack small data files into fewer, larger ones (one atomic snapshot)."""
    req = req or OptimizeRequest()
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    result = _run(
        iceberg_maintenance.compact_table, db, table, _overrides(req), dry_run=req.dry_run
    )
    return {"ok": True, **result}


@router.post(_TABLE + "/iceberg/vacuum")
def vacuum_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: VacuumRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    """VACUUM: expire snapshots past retention and delete unreferenced / orphan files."""
    req = req or VacuumRequest()
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    result = _run(iceberg_maintenance.vacuum_table, db, table, _overrides(req), dry_run=req.dry_run)
    return {"ok": True, **result}


@router.get(_TABLE + "/iceberg/maintenance")
def get_maintenance(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id)
    return {
        "policy": iceberg_maintenance.get_policy(table),
        "defaults": iceberg_maintenance.DEFAULT_POLICY,
        "last_run": (table.iceberg.maintenance or {}).get("last_run"),
    }


@router.put(_TABLE + "/iceberg/maintenance")
def update_maintenance(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: MaintenancePolicyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    policy = _run(iceberg_maintenance.set_policy, db, table, req.model_dump(exclude_none=True))
    return {"ok": True, "policy": policy}


@router.post(_TABLE + "/iceberg/maintenance/run")
def run_maintenance(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: MaintenanceRunRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    req = req or MaintenanceRunRequest()
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    report = _run(
        iceberg_maintenance.run_table_maintenance,
        db,
        table,
        dry_run=req.dry_run,
        force=req.force,
    )
    return {"ok": report["status"] != "error", **report}


# ── Parquet → Iceberg migration ───────────────────────────────────────────────


@router.post("/catalogs/{catalog_id}/migrate-to-iceberg")
def migrate_catalog(
    catalog_id: str,
    req: MigrateRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    """Zero-copy, in-place migration of a catalog's Parquet tables (or one `table`) to Iceberg.

    `dry_run` reports what would be migrated (and why tables would be skipped) without
    writing anything; `verify` checks row counts and columns before each table is switched.
    """
    req = req or MigrateRequest()
    catalog = _resolve_catalog(
        db, user, ws_ctx, catalog_id, require="write" if req.dry_run else "owner"
    )
    result = _run(
        iceberg_migration.migrate_catalog,
        db,
        catalog,
        table=req.table,
        dry_run=req.dry_run,
        verify=req.verify,
    )
    logger.info(
        "User %s ran Iceberg migration on catalog %s (dry_run=%s): %s",
        user.email,
        catalog.slug,
        req.dry_run,
        result["summary"],
    )
    return {"ok": "failed" not in result["summary"], **result}


@router.post(_TABLE + "/migrate-to-iceberg")
def migrate_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: MigrateRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    req = req or MigrateRequest()
    table = _resolve_table(
        db, user, ws_ctx, catalog_id, schema_id, table_id, require="write", iceberg=False
    )
    result = _run(
        iceberg_migration.migrate_table, db, table, dry_run=req.dry_run, verify=req.verify
    )
    return {"ok": result["status"] in ("migrated", "planned"), **result}


@router.post(_TABLE + "/iceberg/rollback-migration")
def rollback_migration(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    table = _resolve_table(db, user, ws_ctx, catalog_id, schema_id, table_id, require="write")
    return {"ok": True, **_run(iceberg_migration.rollback_migration, db, table)}


# ── Internal endpoints for the nightly maintenance DAG ────────────────────────


def require_internal_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    if not x_internal_secret or not hmac.compare_digest(
        x_internal_secret.encode("utf-8"), SECRET_KEY.encode("utf-8")
    ):
        raise HTTPException(403, "Internal access only")


@internal_router.get("/maintenance/tables", dependencies=[Depends(require_internal_secret)])
def internal_list_tables(db: Session = Depends(get_db)):
    """Every Iceberg table across workspaces, for the scheduled maintenance pipeline."""
    tables = iceberg_maintenance.list_iceberg_tables(db)
    return {
        "tables": [
            {
                "table_id": str(t.id),
                "table": t.slug,
                "schema": t.schema.slug,
                "catalog": t.schema.catalog.slug,
                "workspace": getattr(t.schema.catalog.owner_workspace, "slug", None),
                "enabled": iceberg_maintenance.get_policy(t)["enabled"],
            }
            for t in tables
        ]
    }


@internal_router.post(
    "/maintenance/tables/{table_id}/run", dependencies=[Depends(require_internal_secret)]
)
def internal_run_table(
    table_id: str, req: MaintenanceRunRequest | None = None, db: Session = Depends(get_db)
):
    req = req or MaintenanceRunRequest()
    tbl_uuid = _uuid(table_id)
    table = db.query(CatalogTable).filter_by(id=tbl_uuid).first() if tbl_uuid else None
    if not table or table.format != "iceberg" or not table.iceberg:
        raise HTTPException(404, "Iceberg table not found")
    return _run(iceberg_maintenance.run_table_maintenance, db, table, dry_run=req.dry_run)
