from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
ui_dir = str(Path(__file__).parents[1] / "ui")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import auth  # noqa: E402
import vector_index_service  # noqa: E402
from models import Catalog, CatalogAccess, CatalogTable, User  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_vector", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_vector"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def test_sync_table_vector_index_no_s3_path():
    mock_table = MagicMock(spec=CatalogTable)
    mock_table.s3_path_pattern = ""
    mock_db = MagicMock()
    res = vector_index_service.sync_table_vector_index(mock_table, mock_db)
    assert res["ok"] is False
    assert "no S3 storage path" in res["error"]


def test_sync_table_vector_index_no_gemini_key():
    mock_table = MagicMock(spec=CatalogTable)
    mock_table.s3_path_pattern = "s3://datalake/raw/test/*.parquet"
    mock_table.vector_index = {"enabled": True}
    mock_db = MagicMock()
    with patch("vector_index_service.get_stored_gemini_api_key", return_value=None):
        res = vector_index_service.sync_table_vector_index(mock_table, mock_db)
        assert res["ok"] is False
        assert "Gemini API key is not configured" in res["error"]


def test_sync_table_vector_index_success(tmp_path: Path):
    parquet_path = tmp_path / "test_sync.parquet"
    import duckdb

    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT '1' as id, 'Inception' as title, 'Dream thief' as overview, '2010' as release_date)
        TO '{parquet_path}' (FORMAT PARQUET)
    """)
    con.close()

    mock_table = MagicMock(spec=CatalogTable)
    mock_table.slug = "test-movies"
    mock_table.s3_path_pattern = str(parquet_path)
    mock_table.vector_index = {
        "enabled": True,
        "source_column": "overview",
        "format_template": "Movie: {title} | Overview: {overview}",
    }
    mock_db = MagicMock()

    mock_genai_client = MagicMock()
    mock_embed_resp = MagicMock()
    mock_embedding = MagicMock()
    mock_embedding.values = [0.1] * 768
    mock_embed_resp.embeddings = [mock_embedding]
    mock_genai_client.models.embed_content.return_value = mock_embed_resp

    out_path = tmp_path / "out_vector.parquet"
    with (
        patch("vector_index_service.get_stored_gemini_api_key", return_value="dummy_key"),
        patch("vector_index_service.genai.Client", return_value=mock_genai_client),
    ):
        res = vector_index_service.sync_table_vector_index(
            mock_table, mock_db, dest_s3=str(out_path)
        )
        assert res["ok"] is True
        assert res["status"] == "synced"
        assert res["total_vectors"] == 1
        assert "last_synced_at" in res
        assert mock_db.commit.called
        assert out_path.exists()


def test_backend_sync_endpoint():
    user_id = uuid.uuid4()
    cat_id = uuid.uuid4()
    sch_id = uuid.uuid4()
    tbl_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    user = User(id=user_id, email="admin@lake.local", role="admin", is_active=True)
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    catalog = Catalog(id=cat_id, name="default", slug="default", owner_workspace_id=ws_id)
    access = CatalogAccess(catalog_id=cat_id, workspace_id=ws_id, mode="write", status="approved")
    table = CatalogTable(
        id=tbl_id,
        schema_id=sch_id,
        name="Trending",
        slug="trending",
        s3_path_pattern="s3://datalake/raw/test/*.parquet",
        vector_index={"enabled": True},
        schema_drift=False,
        created_at=datetime.now(UTC),
    )

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [catalog, access]
    mock_db.query.return_value.filter_by.return_value.first.return_value = table

    backend_main.app.dependency_overrides[backend_main.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[backend_main.get_current_user] = lambda: user

    try:
        with patch(
            "vector_index_service.sync_table_vector_index",
            return_value={"ok": True, "total_vectors": 10, "status": "synced"},
        ):
            resp = client.post(
                f"/api/catalogs/{cat_id}/schemas/{sch_id}/tables/{tbl_id}/vector-index/sync",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["slug"] == "trending"
    finally:
        backend_main.app.dependency_overrides.clear()
