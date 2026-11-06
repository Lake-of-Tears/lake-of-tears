"""Service for Catalog 7-day soft-delete, UNDROP recovery, and storage lifecycle."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from catalog_storage import (
    delete_prefix,
    is_pattern_in_workspace,
    move_prefix,
    s3_prefix_for_pattern,
)
from fastapi import HTTPException, status
from models import (
    Catalog,
    CatalogSchema,
    CatalogTable,
    DeletedCatalogAsset,
    PipelineMetadata,
    SystemSetting,
    User,
    Workspace,
)
from pipeline_service import get_airflow_dags_dir
from schemas import (
    AssetDependenciesResponse,
    AssetDependency,
    DeletedCatalogAssetResponse,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.catalog_trash")

_DEFAULT_SOFT_DELETE_DAYS = 7
_MEDALLION_TIERS = {"bronze", "silver", "gold"}


def get_soft_delete_days(db: Session) -> int:
    """Read the configured soft delete retention days from system settings."""
    setting = (
        db.query(SystemSetting).filter(SystemSetting.key == "catalog_soft_delete_days").first()
    )
    if setting is not None and setting.value is not None:
        try:
            return int(setting.value)
        except (ValueError, TypeError):
            pass
    return _DEFAULT_SOFT_DELETE_DAYS


def is_managed_table(s3_path_pattern: str | None, workspace_slug: str) -> bool:
    """Return True if the table's S3 pattern resides in the workspace managed prefix."""
    if not s3_path_pattern or not s3_path_pattern.strip():
        return False
    return is_pattern_in_workspace(s3_path_pattern.strip(), workspace_slug)


def check_table_dependencies(
    table: CatalogTable,
    schema: CatalogSchema,
    catalog: Catalog,
    workspace: Workspace,
    db: Session,
) -> AssetDependenciesResponse:
    """Inspect Airflow DAGs and workspace pipeline metadata for references to the table."""
    dependencies: list[AssetDependency] = []
    target_ident = f"{schema.slug}.{table.slug}"
    patterns = [
        target_ident.lower(),
        table.slug.lower(),
        table.name.lower(),
    ]
    if table.s3_path_pattern:
        clean_pat = table.s3_path_pattern.strip().lower()
        if clean_pat:
            patterns.append(clean_pat)

    # 1. Check workspace DAG files
    try:
        dags_dir = get_airflow_dags_dir()
        if dags_dir.exists() and dags_dir.is_dir():
            ws_prefix = f"ws_{workspace.slug}_"
            for py_path in dags_dir.glob("*.py"):
                # Workspace scoped or general DAG
                is_relevant = py_path.name.startswith(ws_prefix) or py_path.name.startswith("dag_")
                if not is_relevant:
                    continue
                try:
                    content = py_path.read_text(encoding="utf-8", errors="ignore")
                    content_lower = content.lower()
                    for pat in patterns:
                        if pat in content_lower:
                            # Extract matching snippet
                            snippet = None
                            for line in content.splitlines():
                                if pat in line.lower():
                                    snippet = line.strip()[:160]
                                    break
                            dependencies.append(
                                AssetDependency(
                                    id=py_path.stem,
                                    name=py_path.name,
                                    type="dag",
                                    file_path=str(py_path),
                                    match_snippet=snippet,
                                )
                            )
                            break
                except Exception as exc:
                    logger.debug("Failed scanning DAG file %s: %s", py_path, exc)
    except Exception as exc:
        logger.debug("Error checking Airflow DAGs directory: %s", exc)

    # 2. Check workspace PipelineMetadata
    try:
        pipelines = db.query(PipelineMetadata).filter_by(workspace_id=workspace.id).all()
        for p in pipelines:
            meta_text = f"{p.dag_id} {p.display_label or ''} {p.description or ''}".lower()
            if any(pat in meta_text for pat in patterns):
                dependencies.append(
                    AssetDependency(
                        id=str(p.id),
                        name=p.display_label or p.dag_id,
                        type="pipeline",
                        file_path=None,
                        match_snippet=p.description[:160] if p.description else None,
                    )
                )
    except Exception as exc:
        logger.debug("Error checking PipelineMetadata: %s", exc)

    is_managed = is_managed_table(table.s3_path_pattern, workspace.slug)
    return AssetDependenciesResponse(
        target_type="table",
        target_name=table.name,
        is_managed=is_managed,
        child_tables_count=0,
        dependencies=dependencies,
        has_dependencies=len(dependencies) > 0,
    )


def check_schema_dependencies(
    schema: CatalogSchema,
    catalog: Catalog,
    workspace: Workspace,
    db: Session,
) -> AssetDependenciesResponse:
    """Inspect Airflow DAGs and workspace pipeline metadata for references to the schema."""
    dependencies: list[AssetDependency] = []
    patterns = [
        schema.slug.lower(),
        schema.name.lower(),
        f"{catalog.slug}.{schema.slug}".lower(),
    ]

    try:
        dags_dir = get_airflow_dags_dir()
        if dags_dir.exists() and dags_dir.is_dir():
            ws_prefix = f"ws_{workspace.slug}_"
            for py_path in dags_dir.glob("*.py"):
                is_relevant = py_path.name.startswith(ws_prefix) or py_path.name.startswith("dag_")
                if not is_relevant:
                    continue
                try:
                    content = py_path.read_text(encoding="utf-8", errors="ignore")
                    content_lower = content.lower()
                    for pat in patterns:
                        if pat in content_lower:
                            snippet = None
                            for line in content.splitlines():
                                if pat in line.lower():
                                    snippet = line.strip()[:160]
                                    break
                            dependencies.append(
                                AssetDependency(
                                    id=py_path.stem,
                                    name=py_path.name,
                                    type="dag",
                                    file_path=str(py_path),
                                    match_snippet=snippet,
                                )
                            )
                            break
                except Exception as exc:
                    logger.debug("Failed scanning DAG file %s: %s", py_path, exc)
    except Exception as exc:
        logger.debug("Error checking Airflow DAGs directory: %s", exc)

    child_count = len(schema.tables) if schema.tables else 0
    return AssetDependenciesResponse(
        target_type="schema",
        target_name=schema.name,
        is_managed=True,
        child_tables_count=child_count,
        dependencies=dependencies,
        has_dependencies=len(dependencies) > 0,
    )


def soft_delete_table(
    db: Session,
    table: CatalogTable,
    schema: CatalogSchema,
    catalog: Catalog,
    workspace: Workspace,
    user_id: uuid.UUID | None,
    batch_id: uuid.UUID | None = None,
) -> DeletedCatalogAsset:
    """Soft-delete a table into deleted_catalog_assets and move managed storage to trash."""
    now = datetime.now(UTC)
    ttl_days = get_soft_delete_days(db)
    purge_at = now + timedelta(days=ttl_days)

    is_managed = is_managed_table(table.s3_path_pattern, workspace.slug)
    trash_prefix = None
    trash_embeddings_prefix = None

    if is_managed and table.s3_path_pattern:
        src_prefix = s3_prefix_for_pattern(table.s3_path_pattern)
        if src_prefix:
            dst_prefix = f"workspaces/{workspace.slug}/.trash/{table.id}/"
            try:
                move_prefix(src_prefix, dst_prefix)
                trash_prefix = dst_prefix
            except Exception as exc:
                logger.warning("Failed moving managed table data to trash: %s", exc)

    # Move vector embeddings if table had vector sync
    v_cfg = table.vector_index or {}
    if v_cfg.get("status") == "synced" or v_cfg.get("dest_s3"):
        emb_prefix = f"workspaces/{workspace.slug}/embeddings/{table.slug}/"
        dst_emb_prefix = f"workspaces/{workspace.slug}/.trash/{table.id}_embeddings/"
        try:
            moved_cnt = move_prefix(emb_prefix, dst_emb_prefix)
            if moved_cnt > 0:
                trash_embeddings_prefix = dst_emb_prefix
        except Exception as exc:
            logger.warning("Failed moving table embeddings to trash: %s", exc)

    deleted_asset = DeletedCatalogAsset(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        catalog_id=catalog.id,
        schema_id=schema.id,
        asset_type="table",
        name=table.name,
        slug=table.slug,
        tier=None,
        description=table.description,
        s3_path_pattern=table.s3_path_pattern,
        column_defs=table.column_defs,
        schema_drift=table.schema_drift,
        vector_index=table.vector_index,
        is_managed=is_managed,
        trash_s3_prefix=trash_prefix,
        trash_embeddings_prefix=trash_embeddings_prefix,
        batch_id=batch_id,
        dropped_by=user_id,
        dropped_at=now,
        purge_at=purge_at,
    )

    db.add(deleted_asset)
    db.delete(table)
    return deleted_asset


def soft_delete_schema(
    db: Session,
    schema: CatalogSchema,
    catalog: Catalog,
    workspace: Workspace,
    user_id: uuid.UUID | None,
    cascade: bool = False,
) -> list[DeletedCatalogAsset]:
    """Soft-delete a schema (and child tables if cascade=True) into deleted_catalog_assets."""
    if schema.tier in _MEDALLION_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete the locked '{schema.tier}' medallion schema",
        )

    child_tables = list(schema.tables or [])
    if child_tables and not cascade:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Schema '{schema.name}' contains {len(child_tables)} table(s). Use CASCADE to delete schema and all child assets.",
        )

    now = datetime.now(UTC)
    ttl_days = get_soft_delete_days(db)
    purge_at = now + timedelta(days=ttl_days)
    batch_id = uuid.uuid4()

    deleted_assets: list[DeletedCatalogAsset] = []

    # 1. Soft-delete all child tables
    for tbl in child_tables:
        del_tbl = soft_delete_table(
            db=db,
            table=tbl,
            schema=schema,
            catalog=catalog,
            workspace=workspace,
            user_id=user_id,
            batch_id=batch_id,
        )
        deleted_assets.append(del_tbl)

    schema.tables.clear()
    db.flush()

    # 2. Record deleted schema
    del_schema = DeletedCatalogAsset(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        catalog_id=catalog.id,
        schema_id=None,
        asset_type="schema",
        name=schema.name,
        slug=schema.slug,
        tier=schema.tier,
        description=schema.description,
        s3_path_pattern=None,
        column_defs=None,
        schema_drift=False,
        vector_index=None,
        is_managed=True,
        trash_s3_prefix=None,
        trash_embeddings_prefix=None,
        batch_id=batch_id,
        dropped_by=user_id,
        dropped_at=now,
        purge_at=purge_at,
    )
    db.add(del_schema)
    deleted_assets.append(del_schema)

    # 3. Remove active schema record
    db.delete(schema)
    return deleted_assets


def restore_asset(
    db: Session,
    asset_id: uuid.UUID,
    workspace: Workspace,
) -> dict[str, Any]:
    """Restore a soft-deleted table or schema (with its cascaded children) from trash."""
    asset = (
        db.query(DeletedCatalogAsset)
        .filter_by(id=asset_id, workspace_id=workspace.id)
        .first()
    )
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deleted asset not found")

    catalog = db.query(Catalog).filter_by(id=asset.catalog_id).first()
    if not catalog:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parent catalog no longer exists",
        )

    if asset.asset_type == "schema":
        # Check for slug collision
        existing_schema = (
            db.query(CatalogSchema)
            .filter_by(catalog_id=catalog.id, slug=asset.slug)
            .first()
        )
        if existing_schema:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Schema with slug '{asset.slug}' already exists in catalog '{catalog.name}'",
            )

        new_schema = CatalogSchema(
            id=uuid.uuid4(),
            catalog_id=catalog.id,
            name=asset.name,
            slug=asset.slug,
            description=asset.description,
            tier=asset.tier or "bronze",
        )
        db.add(new_schema)
        db.flush()

        # Restore cascaded child tables in the same batch
        restored_tables = 0
        if asset.batch_id:
            batch_tables = (
                db.query(DeletedCatalogAsset)
                .filter_by(batch_id=asset.batch_id, asset_type="table")
                .all()
            )
            for child in batch_tables:
                _restore_table_record(db, child, new_schema, workspace)
                restored_tables += 1

        db.delete(asset)
        db.commit()
        return {
            "ok": True,
            "message": f"Restored schema '{asset.name}' and {restored_tables} child table(s)",
            "schema_id": str(new_schema.id),
            "restored_tables_count": restored_tables,
        }

    elif asset.asset_type == "table":
        # Resolve target schema
        schema = None
        if asset.schema_id:
            schema = db.query(CatalogSchema).filter_by(id=asset.schema_id).first()
        if not schema:
            # Fallback to default schema in catalog
            schema = (
                db.query(CatalogSchema)
                .filter_by(catalog_id=catalog.id)
                .order_by(CatalogSchema.created_at.asc())
                .first()
            )
        if not schema:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No suitable active schema found in catalog to restore table into",
            )

        # Check for slug collision in target schema
        existing_table = (
            db.query(CatalogTable)
            .filter_by(schema_id=schema.id, slug=asset.slug)
            .first()
        )
        if existing_table:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Table with slug '{asset.slug}' already exists in schema '{schema.name}'",
            )

        table_obj = _restore_table_record(db, asset, schema, workspace)
        db.commit()
        return {
            "ok": True,
            "message": f"Restored table '{asset.name}' in schema '{schema.name}'",
            "table_id": str(table_obj.id),
            "schema_id": str(schema.id),
        }

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported asset type")


def _restore_table_record(
    db: Session,
    asset: DeletedCatalogAsset,
    schema: CatalogSchema,
    workspace: Workspace,
) -> CatalogTable:
    """Internal helper to move storage back and recreate a CatalogTable record."""
    # Move S3 data back if it was in trash
    if asset.is_managed and asset.trash_s3_prefix and asset.s3_path_pattern:
        orig_prefix = s3_prefix_for_pattern(asset.s3_path_pattern)
        if orig_prefix:
            try:
                move_prefix(asset.trash_s3_prefix, orig_prefix)
            except Exception as exc:
                logger.warning("Failed moving table data back from trash: %s", exc)

    # Move embeddings back if present
    if asset.trash_embeddings_prefix:
        orig_emb_prefix = f"workspaces/{workspace.slug}/embeddings/{asset.slug}/"
        try:
            move_prefix(asset.trash_embeddings_prefix, orig_emb_prefix)
        except Exception as exc:
            logger.warning("Failed moving embeddings back from trash: %s", exc)

    table_obj = CatalogTable(
        id=uuid.uuid4(),
        schema_id=schema.id,
        name=asset.name,
        slug=asset.slug,
        description=asset.description,
        s3_path_pattern=asset.s3_path_pattern,
        column_defs=asset.column_defs or [],
        schema_drift=asset.schema_drift,
        vector_index=asset.vector_index or {},
    )
    db.add(table_obj)
    db.delete(asset)
    return table_obj


def purge_asset(db: Session, asset: DeletedCatalogAsset) -> None:
    """Permanently delete an asset's storage from trash and remove its archive record."""
    if asset.trash_s3_prefix:
        try:
            delete_prefix(asset.trash_s3_prefix)
        except Exception as exc:
            logger.warning("Failed deleting trash prefix %s: %s", asset.trash_s3_prefix, exc)

    if asset.trash_embeddings_prefix:
        try:
            delete_prefix(asset.trash_embeddings_prefix)
        except Exception as exc:
            logger.warning(
                "Failed deleting trash embeddings prefix %s: %s",
                asset.trash_embeddings_prefix,
                exc,
            )

    db.delete(asset)


def purge_expired_assets(db: Session) -> int:
    """Purge all soft-deleted assets whose purge_at is in the past."""
    now = datetime.now(UTC)
    all_assets = db.query(DeletedCatalogAsset).all()
    count = 0
    for asset in all_assets:
        purge_dt = asset.purge_at
        if purge_dt.tzinfo is None:
            purge_dt = purge_dt.replace(tzinfo=UTC)
        if purge_dt <= now:
            purge_asset(db, asset)
            count += 1
    if count > 0:
        db.commit()
        logger.info("Purged %d expired catalog assets from trash", count)
    return count


def list_workspace_trash(
    db: Session, workspace_id: uuid.UUID
) -> list[DeletedCatalogAssetResponse]:
    """Return all soft-deleted catalog assets in the workspace with TTL countdown."""
    assets = (
        db.query(DeletedCatalogAsset)
        .filter_by(workspace_id=workspace_id)
        .order_by(DeletedCatalogAsset.dropped_at.desc())
        .all()
    )
    now = datetime.now(UTC)
    result = []
    for a in assets:
        # Calculate remaining days
        purge_dt = a.purge_at
        if purge_dt.tzinfo is None:
            purge_dt = purge_dt.replace(tzinfo=UTC)
        delta = purge_dt - now
        days_rem = max(0.0, round(delta.total_seconds() / 86400.0, 1))


        cat_name = a.catalog.name if a.catalog else None
        schema_name = None
        if a.schema_id:
            sch = db.query(CatalogSchema).filter_by(id=a.schema_id).first()
            if sch:
                schema_name = sch.name

        resp = DeletedCatalogAssetResponse(
            id=a.id,
            workspace_id=a.workspace_id,
            catalog_id=a.catalog_id,
            catalog_name=cat_name,
            schema_id=a.schema_id,
            schema_name=schema_name,
            asset_type=a.asset_type,
            name=a.name,
            slug=a.slug,
            tier=a.tier,
            description=a.description,
            s3_path_pattern=a.s3_path_pattern,
            column_defs=a.column_defs,
            schema_drift=a.schema_drift,
            vector_index=a.vector_index,
            is_managed=a.is_managed,
            trash_s3_prefix=a.trash_s3_prefix,
            trash_embeddings_prefix=a.trash_embeddings_prefix,
            batch_id=a.batch_id,
            dropped_by=a.dropped_by,
            dropped_by_email=a.dropper.email if a.dropper else None,
            dropped_at=a.dropped_at,
            purge_at=a.purge_at,
            days_remaining=days_rem,
        )
        result.append(resp)
    return result


def try_handle_undrop_statement(
    sql: str,
    workspace: Workspace,
    user: User,
    default_catalog: str | None,
    default_schema: str | None,
    db: Session,
) -> tuple[bool, str | None]:
    """Intercept UNDROP TABLE or UNDROP SCHEMA statements.

    Returns:
        (is_undrop, message_or_error)
    """
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    match = re.match(r"^UNDROP\s+(TABLE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?([^\s;]+)", cleaned, re.IGNORECASE)
    if not match:
        return False, None

    asset_type = match.group(1).lower()
    raw_target = match.group(2).strip().strip('"').strip("'")

    # Split qualified name e.g. cat.sch.tbl or sch.tbl or tbl
    parts = [p.strip().strip('"').strip("'") for p in raw_target.split(".")]
    target_catalog_slug = None
    target_schema_slug = None
    target_name_slug = None

    if len(parts) == 3:
        target_catalog_slug, target_schema_slug, target_name_slug = parts
    elif len(parts) == 2:
        if asset_type == "table":
            target_schema_slug, target_name_slug = parts
        else:
            target_catalog_slug, target_name_slug = parts
    elif len(parts) == 1:
        target_name_slug = parts[0]

    # Resolve against deleted_catalog_assets in this workspace
    q = (
        db.query(DeletedCatalogAsset)
        .filter(
            DeletedCatalogAsset.workspace_id == workspace.id,
            DeletedCatalogAsset.asset_type == asset_type,
            (
                (DeletedCatalogAsset.slug == target_name_slug.lower())
                | (DeletedCatalogAsset.name.ilike(target_name_slug))
            ),
        )
        .order_by(DeletedCatalogAsset.dropped_at.desc())
    )

    asset = q.first()
    if not asset:
        return True, f"Error: No recently deleted {asset_type} named '{raw_target}' found in workspace trash."

    try:
        res = restore_asset(db, asset.id, workspace)
        return True, res.get("message", f"Successfully undropped {asset_type} '{raw_target}'")
    except Exception as exc:
        return True, f"UNDROP {asset_type} failed: {exc}"
