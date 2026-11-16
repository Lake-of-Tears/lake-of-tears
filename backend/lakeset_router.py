"""Lakeset BI Studio Router (`/api/v1/lakeset` and `/api/lakeset`).

Provides:
- `GET /status`: Lakeset DuckDB Lakehouse connection & workspace sync status.
- `GET /datasets`: RBAC-scoped workspace & shared catalog datasets with column descriptions.
- `POST /sync`: Automated DuckDB + MinIO S3 database registration & semantic dataset sync.
- `POST /guest-token`: Short-lived Superset Guest Token minting with per-workspace RLS.
- `POST /explore-data`: Interactive Chart Builder series aggregation & SQL preview.
"""

from __future__ import annotations

import logging
from typing import Any

import ai_service
from auth import get_current_user, get_dashboard_workspace_and_role
from database import get_db
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
)
from models import User, Workspace, WorkspaceDashboard
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from superset_service import (
    _LAST_SYNC_METADATA,
    LAKESET_DB_NAME,
    get_authorized_workspace_tables,
    headless_superset,
    mint_lakeset_guest_token,
    sync_workspace_to_lakeset,
)
from table_scan import ensure_iceberg_loaded, is_iceberg, table_scan_for

logger = logging.getLogger("lake.backend.lakeset.router")

router = APIRouter()


class LakesetSyncRequest(BaseModel):
    catalog_slug: str | None = None
    schema_slug: str | None = None
    force: bool = True


class LakesetGuestTokenRequest(BaseModel):
    dataset: str | None = None
    dashboard_id: str | None = None
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class LakesetExploreRequest(BaseModel):
    dataset: str | None = None
    sql: str | None = None
    chart_type: str = "bar"
    dimension: str | None = None
    metric: str | None = "count"
    metric_agg: str = "count"
    limit: int = Field(default=50, ge=1, le=500)


def _resolve_lakeset_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Workspace, str, User]:
    slug = workspace_slug or x_workspace_slug
    ws_id = x_workspace_id or lake_active_workspace or lake_workspace_id
    ws, role = get_dashboard_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    return ws, role, current_user


@router.get("/status")
def get_lakeset_status(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return Lakeset BI Studio health, DuckDB Lakehouse status, and workspace dataset counts."""
    ws, role, user = ctx
    datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)
    synced_count = sum(1 for d in datasets if d.get("synced"))
    dashboards_count = (
        db.query(WorkspaceDashboard).filter(WorkspaceDashboard.workspace_id == ws.id).count()
    )
    last_sync = _LAST_SYNC_METADATA.get(str(ws.id), {})

    return {
        "ok": True,
        "superset_online": headless_superset.is_healthy(),
        "database_name": LAKESET_DB_NAME,
        "engine": "duckdb",
        "workspace_id": str(ws.id),
        "workspace_slug": ws.slug,
        "workspace_name": ws.name,
        "role": role,
        "user_email": user.email,
        "datasets_count": len(datasets),
        "synced_datasets_count": synced_count,
        "dashboards_count": dashboards_count,
        "last_synced_at": last_sync.get("synced_at"),
    }


@router.get("/datasets")
def list_lakeset_datasets(
    search: str | None = Query(None),
    schema: str | None = Query(None),
    tier: str | None = Query(None),
    catalog: str | None = Query(None),
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List governed catalog datasets authorized for the caller in the active workspace."""
    ws, role, user = ctx
    datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)

    if catalog:
        c_filter = catalog.strip().lower()
        datasets = [
            d
            for d in datasets
            if d["catalog_slug"].lower() == c_filter or d["catalog_name"].lower() == c_filter
        ]
    if schema:
        s_filter = schema.strip().lower()
        datasets = [
            d
            for d in datasets
            if d["schema_slug"].lower() == s_filter or d["schema_name"].lower() == s_filter
        ]
    if tier:
        t_filter = tier.strip().lower()
        datasets = [d for d in datasets if (d.get("tier") or "").lower() == t_filter]
    if search:
        q = search.strip().lower()
        datasets = [
            d
            for d in datasets
            if q in d["full_name"].lower()
            or q in d["display_name"].lower()
            or q in (d.get("description") or "").lower()
            or any(
                q in c["column_name"].lower() or q in (c.get("description") or "").lower()
                for c in d.get("columns", [])
            )
        ]

    return {
        "ok": True,
        "workspace_id": str(ws.id),
        "workspace_slug": ws.slug,
        "role": role,
        "database_name": LAKESET_DB_NAME,
        "total": len(datasets),
        "datasets": datasets,
    }


@router.post("/sync")
def trigger_lakeset_sync(
    req: LakesetSyncRequest | None = None,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Register the DuckDB + MinIO S3 database in Superset and sync workspace catalog tables."""
    ws, _, user = ctx
    result = sync_workspace_to_lakeset(db=db, workspace=ws, user=user)

    if req and (req.catalog_slug or req.schema_slug):
        filtered = result["datasets"]
        if req.catalog_slug:
            filtered = [
                d for d in filtered if d["catalog_slug"].lower() == req.catalog_slug.strip().lower()
            ]
        if req.schema_slug:
            filtered = [
                d for d in filtered if d["schema_slug"].lower() == req.schema_slug.strip().lower()
            ]
        result["datasets"] = filtered
        result["synced_datasets_count"] = len(filtered)

    return result


@router.post("/guest-token")
def create_lakeset_guest_token(
    req: LakesetGuestTokenRequest | None = None,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mint a short-lived Superset Guest Token scoped to the caller's authorized workspace datasets."""
    ws, _, user = ctx
    authorized_datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)
    payload = req or LakesetGuestTokenRequest()

    try:
        token_bundle = mint_lakeset_guest_token(
            user=user,
            workspace=ws,
            authorized_datasets=authorized_datasets,
            requested_dataset=payload.dataset,
            requested_dashboard_id=payload.dashboard_id,
            ttl_seconds=payload.ttl_seconds,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {
        "ok": True,
        **token_bundle,
    }


def _execute_live_duckdb_explore(
    sql: str,
    matched_ds: dict[str, Any] | None,
    authorized_datasets: list[dict[str, Any]],
    chart_type: str,
    metric_label: str,
    is_custom_sql: bool = False,
    catalogs: list[dict[str, Any]] | None = None,
    dimension: str | None = None,
    metric: str | None = None,
) -> dict[str, Any] | None:
    """Attempt to execute the analytical SQL query directly against DuckDB + MinIO S3 Parquet."""
    import os

    try:
        import duckdb
    except ImportError:
        return None

    con = duckdb.connect(":memory:")
    try:
        try:
            con.execute("LOAD httpfs;")
        except Exception:
            con.execute("INSTALL httpfs; LOAD httpfs;")

        raw_ep = os.getenv("MINIO_ENDPOINT", "lake-of-tears-minio:9000").strip()
        use_ssl = raw_ep.startswith("https://")
        clean_ep = raw_ep.replace("http://", "").replace("https://", "").rstrip("/")
        ak = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
        sk = os.getenv("MINIO_SECRET_KEY", "minioadminpassword")

        con.execute(f"SET s3_endpoint='{clean_ep}';")
        con.execute(f"SET s3_access_key_id='{ak}';")
        con.execute(f"SET s3_secret_access_key='{sk}';")
        con.execute(f"SET s3_use_ssl={'true' if use_ssl else 'false'};")
        con.execute("SET s3_url_style='path';")

        # When custom SQL is provided, mount all authorized datasets; otherwise mount matched_ds or top 25
        datasets_to_mount = (
            authorized_datasets
            if is_custom_sql
            else ([matched_ds] if matched_ds else authorized_datasets[:25])
        )
        exec_sql = sql.strip()

        for ds in datasets_to_mount:
            if not ds:
                continue
            sch = ds.get("schema_slug") or "public"
            tbl = ds.get("table_name")
            if not tbl:
                continue
            if not ds.get("s3_path_pattern"):
                continue

            try:
                if is_iceberg(ds):
                    ensure_iceberg_loaded(con)
                scan = table_scan_for(ds, union_by_name=True)
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}";')
                con.execute(f'CREATE OR REPLACE VIEW "{sch}"."{tbl}" AS SELECT * FROM {scan};')
                try:
                    con.execute(f'CREATE OR REPLACE VIEW "{tbl}" AS SELECT * FROM {scan};')
                except Exception:
                    pass
            except Exception as view_err:
                logger.debug("Failed to create view for %s.%s: %s", sch, tbl, view_err)

            full_name = ds.get("full_name")
            if full_name and full_name in exec_sql:
                exec_sql = exec_sql.replace(full_name, f'"{sch}"."{tbl}"')

        # Strip catalog qualifiers using AST parser if catalogs are available
        if catalogs:
            exec_sql = ai_service.strip_catalog_qualifiers(exec_sql, catalogs)

        rel = con.execute(exec_sql)
        rows = rel.fetchall()
        description = rel.description or []
        col_names = [col[0] for col in description]
        col_types = [str(col[1]).upper() for col in description]

        # Dynamic series and dimension column mapping
        dim_idx: int | None = None
        metric_idx: int | None = None
        col_name_to_idx = {name.lower(): i for i, name in enumerate(col_names)}

        if dimension and dimension.lower() in col_name_to_idx:
            dim_idx = col_name_to_idx[dimension.lower()]
        if metric and metric.lower() in col_name_to_idx:
            metric_idx = col_name_to_idx[metric.lower()]

        numeric_type_keywords = ("INT", "FLOAT", "DOUBLE", "DEC", "NUM", "REAL", "HUGEINT")
        categorical_type_keywords = (
            "VARCHAR",
            "TEXT",
            "CHAR",
            "DATE",
            "TIME",
            "BOOL",
            "UUID",
            "STRING",
        )

        numeric_indices = [
            i for i, t in enumerate(col_types) if any(k in t for k in numeric_type_keywords)
        ]
        categorical_indices = [
            i for i, t in enumerate(col_types) if any(k in t for k in categorical_type_keywords)
        ]

        if dim_idx is None:
            if categorical_indices:
                dim_idx = categorical_indices[0]
            elif col_names:
                dim_idx = 0

        if metric_idx is None:
            eligible_metrics = [i for i in numeric_indices if i != dim_idx]
            if eligible_metrics:
                metric_idx = eligible_metrics[0]
            elif len(col_names) > 1 and dim_idx == 0:
                metric_idx = 1
            elif numeric_indices:
                metric_idx = numeric_indices[0]

        resolved_metric_label = (
            col_names[metric_idx]
            if metric_idx is not None and metric_idx < len(col_names)
            else metric_label
        )
        resolved_dim_label = (
            col_names[dim_idx]
            if dim_idx is not None and dim_idx < len(col_names)
            else (dimension or "category")
        )

        if not rows:
            return {
                "categories": [],
                "series": [{"name": resolved_metric_label, "data": []}],
                "columns": col_names,
                "dimension": resolved_dim_label,
                "metric": resolved_metric_label,
                "empty": True,
            }

        c_type = (chart_type or "bar").lower()
        if c_type == "kpi":
            first_row = rows[0]
            val_idx = metric_idx if metric_idx is not None and metric_idx < len(first_row) else -1
            raw_val = first_row[val_idx]
            if isinstance(raw_val, int):
                val_str = f"{raw_val:,}"
            elif isinstance(raw_val, float):
                val_str = f"{raw_val:,.2f}"
            else:
                val_str = str(raw_val)
            return {
                "value": val_str,
                "trend": f"Live DuckDB ({len(rows)} row(s))",
                "columns": col_names,
                "dimension": resolved_dim_label,
                "metric": resolved_metric_label,
            }

        if c_type == "pie":
            series_data = []
            for r in rows[:15]:
                label = str(
                    r[dim_idx]
                    if dim_idx is not None and dim_idx < len(r) and r[dim_idx] is not None
                    else "Unknown"
                )[:42]
                val = (
                    float(r[metric_idx])
                    if metric_idx is not None and metric_idx < len(r) and r[metric_idx] is not None
                    else 1.0
                )
                series_data.append({"name": label, "value": round(val, 2)})
            return {
                "series": series_data,
                "columns": col_names,
                "dimension": resolved_dim_label,
                "metric": resolved_metric_label,
            }

        categories: list[str] = []
        values: list[float] = []
        for r in rows[:100]:
            label = str(
                r[dim_idx]
                if dim_idx is not None and dim_idx < len(r) and r[dim_idx] is not None
                else "Unknown"
            )
            if len(label) > 38:
                label = label[:35] + "..."
            categories.append(label)
            if metric_idx is not None and metric_idx < len(r) and r[metric_idx] is not None:
                try:
                    values.append(round(float(r[metric_idx]), 2))
                except (ValueError, TypeError):
                    values.append(0.0)
            else:
                values.append(1.0)

        return {
            "categories": categories,
            "series": [
                {
                    "name": resolved_metric_label,
                    "data": values,
                }
            ],
            "columns": col_names,
            "dimension": resolved_dim_label,
            "metric": resolved_metric_label,
        }
    except Exception as exc:
        if is_custom_sql:
            logger.warning("Live DuckDB custom SQL explore execution failed: %s", exc)
            raise
        logger.debug("Live DuckDB explore query fallback (%s): %s", sql, exc)
        return None
    finally:
        con.close()


@router.post("/explore-data")
def explore_lakeset_dataset(
    req: LakesetExploreRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Generate analytical chart series and SQL preview for a governed dataset in Chart Builder."""
    ws, _, user = ctx
    authorized_datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)
    catalogs = ai_service.get_workspace_catalogs_from_db(db=db, workspace_id=ws.id, user_id=user.id)

    is_custom_sql = bool(req.sql and req.sql.strip())
    matched_ds: dict[str, Any] | None = None
    if req.dataset and req.dataset.strip().lower() not in ("", "null", "custom_sql"):
        target = req.dataset.strip().lower()
        for d in authorized_datasets:
            if (
                d["full_name"].lower() == target
                or d["table_name"].lower() == target
                or d["sql_view"].lower() == target
                or str(d["id"]).lower() == target
            ):
                matched_ds = d
                break
        if not matched_ds and not is_custom_sql:
            raise HTTPException(
                status_code=403,
                detail=f"Dataset '{req.dataset}' is not authorized in workspace '{ws.slug}'.",
            )
    elif authorized_datasets and not is_custom_sql:
        matched_ds = authorized_datasets[0]

    cols = matched_ds.get("columns", []) if matched_ds else []
    dim = req.dimension
    if not dim and cols:
        preferred_dims = (
            "journal",
            "category",
            "overall_status",
            "study_type",
            "article_type",
            "grade",
            "purpose",
            "loan_status",
            "source",
            "event_type",
            "severity",
            "status",
            "tier",
            "pub_year",
            "year",
        )
        col_names_map = {c["column_name"].lower(): c["column_name"] for c in cols}
        for p_dim in preferred_dims:
            if p_dim in col_names_map:
                dim = col_names_map[p_dim]
                break
        if not dim:
            dim = cols[0]["column_name"]
    dim = dim or "category"

    metric_col = (req.metric or "count").strip()
    agg = (req.metric_agg or "count").upper().strip()
    view_name = matched_ds["sql_view"] if matched_ds else "lakehouse.events"

    if is_custom_sql:
        generated_sql = req.sql.strip()
        exec_sql = generated_sql
        metric_label = metric_col
    elif metric_col.lower() == "count" or agg == "COUNT":
        generated_sql = (
            f'SELECT "{dim}" AS "{dim}", COUNT(*) AS "count" '
            f"FROM {view_name} "
            f'WHERE "{dim}" IS NOT NULL '
            f'GROUP BY "{dim}" ORDER BY "count" DESC LIMIT {req.limit}'
        )
        exec_sql = generated_sql
        metric_label = "Row Count"
    else:
        generated_sql = (
            f'SELECT "{dim}" AS "{dim}", {agg}("{metric_col}") AS "{metric_col}" '
            f"FROM {view_name} "
            f'WHERE "{dim}" IS NOT NULL '
            f'GROUP BY "{dim}" ORDER BY "{metric_col}" DESC LIMIT {req.limit}'
        )
        exec_sql = (
            f'SELECT "{dim}" AS "{dim}", {agg}(TRY_CAST("{metric_col}" AS DOUBLE)) AS "{metric_col}" '
            f"FROM {view_name} "
            f'WHERE "{dim}" IS NOT NULL '
            f'GROUP BY "{dim}" ORDER BY 2 DESC NULLS LAST LIMIT {req.limit}'
        )
        metric_label = f"{agg}({metric_col})"

    try:
        live_data = _execute_live_duckdb_explore(
            sql=exec_sql,
            matched_ds=matched_ds,
            authorized_datasets=authorized_datasets,
            chart_type=req.chart_type or "bar",
            metric_label=metric_label,
            is_custom_sql=is_custom_sql,
            catalogs=catalogs,
            dimension=dim if not is_custom_sql or req.dimension else None,
            metric=metric_col if not is_custom_sql or req.metric else None,
        )
    except Exception as exc:
        if is_custom_sql:
            raise HTTPException(
                status_code=400,
                detail=f"DuckDB query execution failed: {exc}",
            )
        live_data = None

    if is_custom_sql:
        widget_data = live_data
    else:
        widget_data = live_data or headless_superset.execute_widget_query(
            {
                "type": req.chart_type or "bar",
                "dimension": dim,
                "metric": metric_col,
            }
        )

    resp_dim = (widget_data.get("dimension") if isinstance(widget_data, dict) else None) or dim
    resp_metric = (
        widget_data.get("metric") if isinstance(widget_data, dict) else None
    ) or metric_col
    resp_cols = (widget_data.get("columns") if isinstance(widget_data, dict) else None) or cols

    return {
        "ok": True,
        "dataset": matched_ds["full_name"] if matched_ds else (req.dataset or "custom_sql"),
        "chart_type": req.chart_type,
        "dimension": resp_dim,
        "metric": resp_metric,
        "columns": resp_cols,
        "sql": generated_sql,
        "data": widget_data,
    }
