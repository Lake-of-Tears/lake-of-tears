"""Native Apache Iceberg REST Catalog for Lake of Tears.

Implements the open Iceberg REST catalog protocol on top of `LotIcebergCatalog`, so PyIceberg,
DuckDB, Trino and Spark can use LoT as their catalog with a LoT token:

    load_catalog("lot", type="rest", uri="https://<host>/api/v1/iceberg",
                 warehouse="<workspace-slug>", token="<personal access token>")

The REST `prefix` is the workspace slug; namespaces are `<catalog>.<schema>`. Storage
credentials are never vended: clients read and write data files with their own S3 settings.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from auth import get_current_user, get_workspace_context
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from iceberg_catalog import LotIcebergCatalog
from models import User
from pydantic import ValidationError
from pyiceberg.catalog.rest import CreateTableRequest, RegisterTableRequest, TableResponse
from pyiceberg.exceptions import (
    CommitFailedException,
    ForbiddenError,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    NoSuchViewError,
    TableAlreadyExistsError,
)
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC
from pyiceberg.table import CommitTableRequest
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.iceberg.rest")

router = APIRouter()

NAMESPACE_SEPARATOR = "\x1f"

SUPPORTED_ENDPOINTS = [
    "GET /v1/{prefix}/namespaces",
    "POST /v1/{prefix}/namespaces",
    "GET /v1/{prefix}/namespaces/{namespace}",
    "HEAD /v1/{prefix}/namespaces/{namespace}",
    "DELETE /v1/{prefix}/namespaces/{namespace}",
    "POST /v1/{prefix}/namespaces/{namespace}/properties",
    "GET /v1/{prefix}/namespaces/{namespace}/tables",
    "POST /v1/{prefix}/namespaces/{namespace}/tables",
    "GET /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "HEAD /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "DELETE /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "POST /v1/{prefix}/namespaces/{namespace}/register",
    "POST /v1/{prefix}/tables/rename",
]

_ERRORS: list[tuple[type[Exception], int, str]] = [
    (NoSuchTableError, 404, "NoSuchTableException"),
    (NoSuchNamespaceError, 404, "NoSuchNamespaceException"),
    (NoSuchViewError, 404, "NoSuchViewException"),
    (TableAlreadyExistsError, 409, "AlreadyExistsException"),
    (NamespaceAlreadyExistsError, 409, "AlreadyExistsException"),
    (NamespaceNotEmptyError, 409, "NamespaceNotEmptyException"),
    (CommitFailedException, 409, "CommitFailedException"),
    (ForbiddenError, 403, "ForbiddenException"),
    (NotImplementedError, 406, "UnsupportedOperationException"),
    (ValidationError, 400, "BadRequestException"),
    (ValueError, 400, "BadRequestException"),
]


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind, "code": status}},
    )


def _handle(exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        kind = {401: "NotAuthorizedException", 403: "ForbiddenException"}.get(
            exc.status_code, "BadRequestException"
        )
        return _error(exc.status_code, kind, str(exc.detail))
    for exc_type, status, kind in _ERRORS:
        if isinstance(exc, exc_type):
            return _error(status, kind, str(exc))
    logger.exception("Iceberg REST catalog request failed: %s", exc)
    return _error(500, "InternalServerError", "Internal catalog error")


def _json(model: Any, status: int = 200) -> Response:
    return Response(
        content=model.model_dump_json(), status_code=status, media_type="application/json"
    )


def _namespace(raw: str) -> tuple[str, ...]:
    sep = NAMESPACE_SEPARATOR if NAMESPACE_SEPARATOR in raw else "."
    return tuple(p for p in raw.split(sep) if p)


def _client_config() -> dict[str, str]:
    """Non-secret FileIO hints returned with tables. Credentials are never vended."""
    config = {"s3.path-style-access": "true"}
    endpoint = os.getenv("ICEBERG_REST_CLIENT_S3_ENDPOINT")
    if endpoint:
        config["s3.endpoint"] = endpoint
    return config


def _catalog(prefix: str, user: User, db: Session) -> LotIcebergCatalog:
    workspace, role = get_workspace_context(workspace_slug=prefix, current_user=user, db=db)
    return LotIcebergCatalog(db, workspace, user, role=role)


def _table_response(table: Any) -> Response:
    return _json(
        TableResponse(
            metadata_location=table.metadata_location,
            metadata=table.metadata,
            config=_client_config(),
        )
    )


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object")
    return parsed


# ── Configuration ─────────────────────────────────────────────────────────────


def _config_response(prefix: str | None, user: User, db: Session) -> Response:
    try:
        slug = (prefix or "").strip()
        if not slug:
            from auth import get_user_accessible_workspaces

            workspaces = get_user_accessible_workspaces(db, user)
            if len(workspaces) != 1:
                raise ValueError(
                    "Set the catalog `warehouse` property to a Lake of Tears workspace slug"
                )
            slug = workspaces[0].slug
        workspace, _role = get_workspace_context(workspace_slug=slug, current_user=user, db=db)
        return JSONResponse(
            {
                "defaults": _client_config(),
                "overrides": {"prefix": workspace.slug},
                "endpoints": SUPPORTED_ENDPOINTS,
            }
        )
    except Exception as exc:
        return _handle(exc)


@router.get("/v1/config")
def get_config(
    warehouse: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _config_response(warehouse, user, db)


@router.get("/v1/{prefix}/config")
def get_prefixed_config(
    prefix: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _config_response(prefix, user, db)


# ── Namespaces ────────────────────────────────────────────────────────────────


@router.get("/v1/{prefix}/namespaces")
def list_namespaces(
    prefix: str,
    parent: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        catalog = _catalog(prefix, user, db)
        namespaces = catalog.list_namespaces(_namespace(parent) if parent else ())
        return {"namespaces": [list(ns) for ns in namespaces]}
    except Exception as exc:
        return _handle(exc)


@router.post("/v1/{prefix}/namespaces")
async def create_namespace(
    prefix: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = await _body(request)
        namespace = tuple(body.get("namespace") or ())
        properties = {str(k): str(v) for k, v in (body.get("properties") or {}).items()}
        catalog = _catalog(prefix, user, db)
        catalog.create_namespace(namespace, properties)
        return {
            "namespace": list(namespace),
            "properties": catalog.load_namespace_properties(namespace),
        }
    except Exception as exc:
        return _handle(exc)


@router.get("/v1/{prefix}/namespaces/{namespace}")
def load_namespace(
    prefix: str,
    namespace: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        ns = _namespace(namespace)
        properties = _catalog(prefix, user, db).load_namespace_properties(ns)
        return {"namespace": list(ns), "properties": properties}
    except Exception as exc:
        return _handle(exc)


@router.head("/v1/{prefix}/namespaces/{namespace}")
def namespace_exists(
    prefix: str,
    namespace: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        exists = _catalog(prefix, user, db).namespace_exists(_namespace(namespace))
        return Response(status_code=204 if exists else 404)
    except Exception as exc:
        return _handle(exc)


@router.delete("/v1/{prefix}/namespaces/{namespace}")
def drop_namespace(
    prefix: str,
    namespace: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        _catalog(prefix, user, db).drop_namespace(_namespace(namespace))
        return Response(status_code=204)
    except Exception as exc:
        return _handle(exc)


@router.post("/v1/{prefix}/namespaces/{namespace}/properties")
async def update_namespace_properties(
    prefix: str,
    namespace: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = await _body(request)
        summary = _catalog(prefix, user, db).update_namespace_properties(
            _namespace(namespace),
            removals=set(body.get("removals") or []),
            updates={str(k): str(v) for k, v in (body.get("updates") or {}).items()},
        )
        return {"removed": summary.removed, "updated": summary.updated, "missing": summary.missing}
    except Exception as exc:
        return _handle(exc)


# ── Tables ────────────────────────────────────────────────────────────────────


@router.get("/v1/{prefix}/namespaces/{namespace}/tables")
def list_tables(
    prefix: str,
    namespace: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        identifiers = _catalog(prefix, user, db).list_tables(_namespace(namespace))
        return {"identifiers": [{"namespace": list(i[:-1]), "name": i[-1]} for i in identifiers]}
    except Exception as exc:
        return _handle(exc)


@router.post("/v1/{prefix}/namespaces/{namespace}/tables")
async def create_table(
    prefix: str,
    namespace: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = await _body(request)
        body.setdefault("location", None)
        body.setdefault("partition-spec", None)
        body.setdefault("write-order", None)
        req = CreateTableRequest.model_validate(body)
        catalog = _catalog(prefix, user, db)
        identifier = (*_namespace(namespace), req.name)
        partition_spec = req.partition_spec or UNPARTITIONED_PARTITION_SPEC
        sort_order = req.write_order or UNSORTED_SORT_ORDER

        if req.stage_create:
            # Nothing is persisted: the client commits the staged metadata with assert-create.
            catalog_row, schema_row, name = catalog._validate_new_table(identifier)
            location = catalog.resolve_new_table_location(
                catalog_row, schema_row, name, req.location
            )
            metadata = new_table_metadata(
                location=location,
                schema=req.table_schema,
                partition_spec=partition_spec,
                sort_order=sort_order,
                properties=req.properties,
            )
            provider = load_location_provider(location, req.properties)
            return _json(
                TableResponse(
                    metadata_location=provider.new_table_metadata_file_location(),
                    metadata=metadata,
                    config=_client_config(),
                )
            )

        table = catalog.create_table(
            identifier,
            schema=req.table_schema,
            location=req.location,
            partition_spec=partition_spec,
            sort_order=sort_order,
            properties=req.properties,
        )
        return _table_response(table)
    except Exception as exc:
        return _handle(exc)


@router.post("/v1/{prefix}/namespaces/{namespace}/register")
async def register_table(
    prefix: str,
    namespace: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = await _body(request)
        body.setdefault("overwrite", False)
        req = RegisterTableRequest.model_validate(body)
        table = _catalog(prefix, user, db).register_table(
            (*_namespace(namespace), req.name), req.metadata_location, overwrite=req.overwrite
        )
        return _table_response(table)
    except Exception as exc:
        return _handle(exc)


@router.get("/v1/{prefix}/namespaces/{namespace}/tables/{table}")
def load_table(
    prefix: str,
    namespace: str,
    table: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return _table_response(
            _catalog(prefix, user, db).load_table((*_namespace(namespace), table))
        )
    except Exception as exc:
        return _handle(exc)


@router.head("/v1/{prefix}/namespaces/{namespace}/tables/{table}")
def table_exists(
    prefix: str,
    namespace: str,
    table: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        exists = _catalog(prefix, user, db).table_exists((*_namespace(namespace), table))
        return Response(status_code=204 if exists else 404)
    except Exception as exc:
        return _handle(exc)


@router.post("/v1/{prefix}/namespaces/{namespace}/tables/{table}")
async def commit_table(
    prefix: str,
    namespace: str,
    table: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = await _body(request)
        identifier = (*_namespace(namespace), table)
        # The path, not the body, is authoritative for which table is committed.
        body["identifier"] = {"namespace": list(identifier[:-1]), "name": table}
        req = CommitTableRequest.model_validate(body)
        result = _catalog(prefix, user, db).commit_identifier(
            identifier, req.requirements, req.updates
        )
        return _json(result)
    except Exception as exc:
        return _handle(exc)


@router.delete("/v1/{prefix}/namespaces/{namespace}/tables/{table}")
def drop_table(
    prefix: str,
    namespace: str,
    table: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        _catalog(prefix, user, db).drop_table((*_namespace(namespace), table))
        return Response(status_code=204)
    except Exception as exc:
        return _handle(exc)


@router.post("/v1/{prefix}/tables/rename")
async def rename_table(
    prefix: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = await _body(request)
        src, dst = body.get("source") or {}, body.get("destination") or {}
        _catalog(prefix, user, db).rename_table(
            (*(src.get("namespace") or ()), src.get("name") or ""),
            (*(dst.get("namespace") or ()), dst.get("name") or ""),
        )
        return Response(status_code=204)
    except Exception as exc:
        return _handle(exc)
