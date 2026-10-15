from __future__ import annotations

import importlib.util
import io
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from starlette.testclient import TestClient

# Ensure backend directory is in sys.path
backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import storage_service  # noqa: E402
from models import User, Workspace, WorkspaceMember  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_storage", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_storage"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


# ── Helpers & Fixtures ────────────────────────────────────────────────────────


def _generate_test_user_and_token(role: str = "viewer", global_role: str = "user"):
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}@lake.local",
        display_name=role.title(),
        role=global_role,
        is_active=True,
    )
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    ws_id = uuid.uuid4()
    workspace = Workspace(
        id=ws_id,
        name="Analytics Workspace",
        slug="analytics",
        status="active",
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    return user, workspace, member, token


def _create_mock_parquet_bytes() -> bytes:
    table = pa.table(
        {
            "id": pa.array([1, 2, 3], type=pa.int64()),
            "metric": pa.array([10.5, 20.2, 30.8], type=pa.float64()),
            "label": pa.array(["alpha", "beta", "gamma"], type=pa.string()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


# ── Storage Service Unit Tests ───────────────────────────────────────────────


def test_human_size():
    assert storage_service._human_size(0) == "0 B"
    assert storage_service._human_size(500) == "500.0 B"
    assert storage_service._human_size(1024) == "1.0 KB"
    assert storage_service._human_size(1024 * 1024) == "1.0 MB"
    assert storage_service._human_size(5 * 1024 * 1024 * 1024) == "5.0 GB"


def test_breadcrumbs_and_parent():
    crumbs = storage_service.build_breadcrumbs("")
    assert len(crumbs) == 1
    assert crumbs[0]["name"] == "datalake"
    assert storage_service.get_parent_prefix("") is None

    crumbs = storage_service.build_breadcrumbs("raw/stripe/year=2026/")
    assert len(crumbs) == 4
    assert crumbs[0]["name"] == "datalake"
    assert crumbs[1]["name"] == "raw"
    assert crumbs[1]["prefix"] == "raw/"
    assert crumbs[2]["name"] == "stripe"
    assert crumbs[2]["prefix"] == "raw/stripe/"
    assert crumbs[3]["name"] == "year=2026"
    assert crumbs[3]["prefix"] == "raw/stripe/year=2026/"

    assert storage_service.get_parent_prefix("raw/stripe/year=2026/") == "raw/stripe/"
    assert storage_service.get_parent_prefix("raw/") == ""


def test_s3_range_reader_parses_parquet():
    parquet_data = _create_mock_parquet_bytes()
    mock_s3 = MagicMock()

    def mock_get_object(Bucket, Key, Range):
        # Range is like "bytes=100-199"
        parts = Range.replace("bytes=", "").split("-")
        start = int(parts[0])
        end = int(parts[1]) + 1
        body_mock = MagicMock()
        body_mock.read.return_value = parquet_data[start:end]
        return {"Body": body_mock}

    mock_s3.get_object.side_effect = mock_get_object

    reader = storage_service.S3RangeReader(mock_s3, "datalake", "test.parquet", len(parquet_data))
    pf = pq.ParquetFile(reader)

    assert pf.metadata.num_rows == 3
    assert pf.metadata.num_columns == 3
    assert pf.schema.names == ["id", "metric", "label"]


def test_browse_prefix_folders_and_files():
    mock_s3 = MagicMock()
    now = datetime.now(UTC)
    mock_s3.list_objects_v2.return_value = {
        "CommonPrefixes": [
            {"Prefix": "raw/stripe/"},
            {"Prefix": "raw/shopify/"},
        ],
        "Contents": [
            {
                "Key": "raw/summary.parquet",
                "Size": 1048576,
                "LastModified": now,
                "ETag": '"abc123etag"',
            },
            {
                "Key": "raw/run.ipynb",
                "Size": 2048,
                "LastModified": now,
                "ETag": '"def456etag"',
            },
            {
                "Key": "raw/notes.txt",
                "Size": 512,
                "LastModified": now,
                "ETag": '"txt789etag"',
            },
        ],
        "IsTruncated": True,
        "NextContinuationToken": "token-xyz-123",
    }

    res = storage_service.browse_prefix(
        bucket="datalake",
        prefix="raw/",
        s3_client=mock_s3,
    )

    assert res["bucket"] == "datalake"
    assert res["prefix"] == "raw/"
    assert res["total_folders"] == 2
    assert res["folders"][0]["name"] == "shopify"  # Sorted alphabetically
    assert res["folders"][1]["name"] == "stripe"

    assert res["total_files"] == 3
    files_by_name = {f["name"]: f for f in res["files"]}

    assert files_by_name["summary.parquet"]["is_parquet"] is True
    assert files_by_name["summary.parquet"]["is_notebook"] is False
    assert files_by_name["summary.parquet"]["size_human"] == "1.0 MB"

    assert files_by_name["run.ipynb"]["is_parquet"] is False
    assert files_by_name["run.ipynb"]["is_notebook"] is True

    assert files_by_name["notes.txt"]["is_parquet"] is False
    assert files_by_name["notes.txt"]["is_notebook"] is False

    assert res["is_truncated"] is True
    assert res["next_continuation_token"] == "token-xyz-123"


def test_get_object_metadata_parquet():
    parquet_data = _create_mock_parquet_bytes()
    mock_s3 = MagicMock()
    now = datetime.now(UTC)

    mock_s3.head_object.return_value = {
        "ContentLength": len(parquet_data),
        "LastModified": now,
        "ContentType": "application/x-parquet",
        "ETag": '"etag-parquet-001"',
        "Metadata": {"source": "hubspot"},
    }

    def mock_get_object(Bucket, Key, Range):
        parts = Range.replace("bytes=", "").split("-")
        start = int(parts[0])
        end = int(parts[1]) + 1
        body_mock = MagicMock()
        body_mock.read.return_value = parquet_data[start:end]
        return {"Body": body_mock}

    mock_s3.get_object.side_effect = mock_get_object

    meta = storage_service.get_object_metadata(
        bucket="datalake",
        key="raw/data.parquet",
        s3_client=mock_s3,
    )

    assert meta["key"] == "raw/data.parquet"
    assert meta["is_parquet"] is True
    assert meta["parquet_metadata"] is not None

    pm = meta["parquet_metadata"]
    assert pm["num_rows"] == 3
    assert pm["num_columns"] == 3
    assert len(pm["columns"]) == 3

    col_names = [c["name"] for c in pm["columns"]]
    assert col_names == ["id", "metric", "label"]

    id_col = next(c for c in pm["columns"] if c["name"] == "id")
    assert id_col["compression"] == "SNAPPY"
    assert id_col["arrow_type"] == "int64"


def test_get_object_metadata_notebook():
    import json

    mock_s3 = MagicMock()
    now = datetime.now(UTC)

    nb_content = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            },
            "papermill": {
                "duration": 5.42,
                "exception": False,
                "status": "completed",
            },
        },
        "cells": [
            {"cell_type": "markdown", "source": "# Analysis"},
            {"cell_type": "code", "execution_count": 1, "source": "print('hello')"},
        ],
    }
    nb_bytes = json.dumps(nb_content).encode("utf-8")

    mock_s3.head_object.return_value = {
        "ContentLength": len(nb_bytes),
        "LastModified": now,
        "ContentType": "application/x-ipynb+json",
        "ETag": '"etag-nb-001"',
        "Metadata": {},
    }

    body_mock = MagicMock()
    body_mock.read.return_value = nb_bytes
    mock_s3.get_object.return_value = {"Body": body_mock}

    meta = storage_service.get_object_metadata(
        bucket="datalake",
        key="notebook_runs/default/root_db/run1.ipynb",
        s3_client=mock_s3,
    )

    assert meta["key"] == "notebook_runs/default/root_db/run1.ipynb"
    assert meta["is_parquet"] is False
    assert meta["is_notebook"] is True
    assert meta["notebook_metadata"] is not None

    nb_info = meta["notebook_metadata"]
    assert nb_info["cells_count"] == 2
    assert nb_info["kernel_name"] == "Python 3 (ipykernel)"
    assert nb_info["language"] == "python"
    assert nb_info["executed_by"] == "Papermill"
    assert nb_info["duration_seconds"] == 5.42
    assert nb_info["has_exception"] is False


def test_get_download_url_remaps_public_endpoint():
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = (
        "http://minio:9000/datalake/raw/test.parquet?X-Amz-Signature=xyz"
    )

    with patch.object(storage_service, "MINIO_PUBLIC_ENDPOINT", "http://localhost:9000"):
        url = storage_service.get_download_url(
            bucket="datalake",
            key="raw/test.parquet",
            s3_client=mock_s3,
        )
        assert url.startswith("http://localhost:9000/datalake/raw/test.parquet")
        assert "X-Amz-Signature=xyz" in url


def test_delete_object():
    mock_s3 = MagicMock()
    res = storage_service.delete_object(
        bucket="datalake",
        key="raw/obsolete.parquet",
        s3_client=mock_s3,
    )
    assert res["deleted"] is True
    assert res["key"] == "raw/obsolete.parquet"
    mock_s3.delete_object.assert_called_once_with(
        Bucket="datalake",
        Key="raw/obsolete.parquet",
    )


# ── Storage Router & RBAC Integration Tests ─────────────────────────────────


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    backend_main.app.dependency_overrides.clear()
    yield
    backend_main.app.dependency_overrides.clear()


def test_unauthenticated_request_rejected():
    backend_main.app.dependency_overrides.clear()
    resp = client.get("/api/v1/storage/browse")
    assert resp.status_code == 401


def test_browse_storage_authenticated():
    user, ws, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    with (
        patch("storage_router.get_storage_workspace_and_role", return_value=(ws, "viewer")),
        patch("storage_service.browse_prefix") as mock_browse,
    ):
        mock_browse.return_value = {
            "bucket": "datalake",
            "prefix": "",
            "folders": [{"name": "raw", "prefix": "raw/"}],
            "files": [],
            "total_folders": 1,
            "total_files": 0,
            "is_truncated": False,
        }

        resp = client.get(
            "/api/v1/storage/browse",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"lake_active_workspace": str(ws.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bucket"] == "datalake"
        assert len(data["folders"]) == 1


def test_storage_stats_endpoint():
    user, ws, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    with (
        patch("storage_router.get_storage_workspace_and_role", return_value=(ws, "viewer")),
        patch("storage_service.get_bucket_summary") as mock_summary,
    ):
        mock_summary.return_value = {
            "ok": True,
            "bucket": "datalake",
            "total_objects": 42,
            "total_size": "100.0 MB",
        }

        resp = client.get(
            "/api/v1/storage/stats",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"lake_active_workspace": str(ws.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["total_objects"] == 42


def test_delete_object_rbac_viewer_blocked():
    user, ws, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    member.role = "viewer"
    mock_db.query.return_value.filter.return_value.first.return_value = member

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    resp = client.delete(
        "/api/v1/storage/objects/raw/stripe/data.parquet",
        headers={"Authorization": f"Bearer {token}"},
        cookies={"lake_active_workspace": str(ws.id)},
    )
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"].lower()


def test_delete_object_rbac_admin_allowed():
    user, ws, member, token = _generate_test_user_and_token(role="admin")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    member.role = "admin"
    mock_db.query.return_value.filter.return_value.first.return_value = member

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    with patch("storage_service.delete_object") as mock_del:
        mock_del.return_value = {"deleted": True, "key": "raw/stripe/data.parquet"}

        resp = client.delete(
            "/api/v1/storage/objects/raw/stripe/data.parquet",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"lake_active_workspace": str(ws.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True


def test_delete_object_rbac_superadmin_allowed():
    user, ws, member, token = _generate_test_user_and_token(role="viewer", global_role="superadmin")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    mock_db.query.return_value.filter.return_value.first.return_value = member

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    with patch("storage_service.delete_object") as mock_del:
        mock_del.return_value = {"deleted": True, "key": "raw/stripe/data.parquet"}

        resp = client.delete(
            "/api/v1/storage/objects/raw/stripe/data.parquet",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"lake_active_workspace": str(ws.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
