from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import uuid
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
from auth import get_current_user
from database import get_db
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from models import User, Workspace, WorkspaceMember, generate_webhook_secret
from schemas import WebhookIngestResponse, WebhookSecretResponse
from sqlalchemy.orm import Session
from storage_service import MINIO_BUCKET, get_s3_client

logger = logging.getLogger("lake.backend.webhooks")

router = APIRouter()

WEBHOOK_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("received_at", pa.timestamp("us", tz="UTC")),
        ("source", pa.string()),
        ("headers", pa.string()),
        ("payload", pa.string()),
    ]
)

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "x-lake-webhook-secret",
    "x-lake-signature",
    "x-hub-signature-256",
}


def _verify_webhook_auth(
    raw_body: bytes,
    secret: str,
    header_secret: str | None,
    query_token: str | None,
    signature_header: str | None,
) -> bool:
    """Verify incoming request using either direct token matching or HMAC-SHA256 signature."""
    if not secret:
        return False

    # 1. Direct secret token verification
    token = header_secret or query_token
    if token and hmac.compare_digest(token.strip(), secret):
        return True

    # 2. HMAC-SHA256 signature verification
    if signature_header:
        sig = signature_header.strip()
        if sig.startswith("sha256="):
            sig = sig[7:]
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(sig.lower(), expected_sig.lower()):
            return True

    return False


def _resolve_workspace_or_404(workspace_slug_or_id: str, db: Session) -> Workspace:
    try:
        ws_id = uuid.UUID(workspace_slug_or_id)
        ws = (
            db.query(Workspace)
            .filter(
                Workspace.id == ws_id,
                Workspace.status == "active",
                Workspace.deleted_at.is_(None),
            )
            .first()
        )
    except ValueError:
        ws = None

    if not ws:
        ws = (
            db.query(Workspace)
            .filter(
                Workspace.slug == workspace_slug_or_id,
                Workspace.status == "active",
                Workspace.deleted_at.is_(None),
            )
            .first()
        )

    if not ws:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace '{workspace_slug_or_id}' not found",
        )
    return ws


def _check_workspace_access(
    workspace: Workspace,
    user: User,
    db: Session,
    require_admin: bool = False,
) -> None:
    if user.role == "superadmin":
        return

    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == user.id,
        )
        .first()
    )
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this workspace",
        )

    if require_admin and member.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace admin privileges required",
        )


@router.post(
    "/{workspace_slug}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookIngestResponse,
)
async def ingest_webhook_event(
    workspace_slug: str,
    request: Request,
    token: str | None = Query(None),
    x_lake_webhook_secret: str | None = Header(None, alias="X-Lake-Webhook-Secret"),
    x_lake_signature: str | None = Header(None, alias="X-Lake-Signature"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    db: Session = Depends(get_db),
) -> WebhookIngestResponse:
    """Accept and buffer incoming webhook events into MinIO for a workspace."""
    ws = _resolve_workspace_or_404(workspace_slug, db)

    # Ensure workspace has a secret configured
    if not ws.webhook_secret:
        ws.webhook_secret = generate_webhook_secret()
        db.commit()

    raw_body = await request.body()
    signature_header = x_lake_signature or x_hub_signature_256

    is_authenticated = _verify_webhook_auth(
        raw_body=raw_body,
        secret=ws.webhook_secret,
        header_secret=x_lake_webhook_secret,
        query_token=token,
        signature_header=signature_header,
    )
    if not is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret/signature",
        )

    # Parse payload
    try:
        payload_data = json.loads(raw_body) if raw_body else {}
    except Exception:
        payload_data = raw_body.decode("utf-8", errors="replace")

    # Sanitize incoming headers for auditing
    clean_headers = {
        k.lower(): v for k, v in request.headers.items() if k.lower() not in SENSITIVE_HEADERS
    }

    event_id = str(uuid.uuid4())
    received_at = datetime.now(UTC)

    # Build Parquet buffer record
    payload_str = (
        json.dumps(payload_data) if isinstance(payload_data, dict | list) else str(payload_data)
    )
    table = pa.Table.from_pydict(
        {
            "event_id": [event_id],
            "received_at": [received_at],
            "source": ["webhook"],
            "headers": [json.dumps(clean_headers)],
            "payload": [payload_str],
        },
        schema=WEBHOOK_SCHEMA,
    )

    out = io.BytesIO()
    pq.write_table(table, out, compression="SNAPPY")
    out.seek(0)

    y = received_at.strftime("%Y")
    m = received_at.strftime("%m")
    d = received_at.strftime("%d")
    s3_key = (
        f"workspaces/{ws.slug}/raw/webhook/buffer/year={y}/month={m}/day={d}/{event_id}.parquet"
    )

    try:
        s3 = get_s3_client()
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=s3_key,
            Body=out.getvalue(),
            ContentType="application/octet-stream",
        )
    except Exception as exc:
        logger.error("Failed to buffer webhook event to MinIO: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Storage buffering failed: {exc}",
        ) from exc

    return WebhookIngestResponse(
        status="accepted",
        event_id=event_id,
        workspace_slug=ws.slug,
        received_at=received_at,
    )


@router.get(
    "/{workspace_slug}/secret",
    response_model=WebhookSecretResponse,
)
def get_webhook_secret(
    workspace_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WebhookSecretResponse:
    """Retrieve the webhook configuration and secret token for a workspace."""
    ws = _resolve_workspace_or_404(workspace_slug, db)
    _check_workspace_access(ws, current_user, db, require_admin=False)

    if not ws.webhook_secret:
        ws.webhook_secret = generate_webhook_secret()
        db.commit()

    return WebhookSecretResponse(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        webhook_secret=ws.webhook_secret,
        webhook_url=f"/api/v1/ingest/webhooks/{ws.slug}",
    )


@router.post(
    "/{workspace_slug}/secret/regenerate",
    response_model=WebhookSecretResponse,
)
def regenerate_webhook_secret(
    workspace_slug: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WebhookSecretResponse:
    """Regenerate the webhook secret for a workspace (admin only)."""
    ws = _resolve_workspace_or_404(workspace_slug, db)
    _check_workspace_access(ws, current_user, db, require_admin=True)

    ws.webhook_secret = generate_webhook_secret()
    db.commit()

    return WebhookSecretResponse(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        webhook_secret=ws.webhook_secret,
        webhook_url=f"/api/v1/ingest/webhooks/{ws.slug}",
    )
