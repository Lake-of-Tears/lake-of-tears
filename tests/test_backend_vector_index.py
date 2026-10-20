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


class FakeProvider:
    """Stands in for the active AI provider: any name, model and vector length."""

    name = "fake"

    def __init__(self, dimension: int = 4, embedding_model: str = "fake-embed"):
        self.dimension = dimension
        self.embedding_model = embedding_model
        self.calls: list[list[str]] = []

    def embed_texts(self, texts, *, query=False):
        self.calls.append(list(texts))
        return [[0.1] * self.dimension for _ in texts]


def _movies_parquet(tmp_path: Path) -> Path:
    import duckdb

    parquet_path = tmp_path / "test_sync.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT '1' as id, 'Inception' as title, 'Dream thief' as overview, '2010' as release_date)
        TO '{parquet_path}' (FORMAT PARQUET)
    """)
    con.close()
    return parquet_path


def _movies_table(parquet_path: Path) -> MagicMock:
    mock_table = MagicMock(spec=CatalogTable)
    mock_table.slug = "test-movies"
    mock_table.s3_path_pattern = str(parquet_path)
    mock_table.vector_index = {
        "enabled": True,
        "source_column": "overview",
        "format_template": "Movie: {title} | Overview: {overview}",
    }
    return mock_table


def test_sync_table_vector_index_reports_an_unconfigured_provider():
    from ai_providers import ProviderNotConfigured

    mock_table = MagicMock(spec=CatalogTable)
    mock_table.s3_path_pattern = "s3://datalake/raw/test/*.parquet"
    mock_table.vector_index = {"enabled": True}
    with patch(
        "vector_index_service.get_active_provider",
        side_effect=ProviderNotConfigured("OpenAI: the API key is not set."),
    ):
        res = vector_index_service.sync_table_vector_index(mock_table, MagicMock())
    assert res["ok"] is False
    assert "API key is not set" in res["error"]
    assert "Gemini" not in res["error"]
    assert mock_table.vector_index["status"] == "error"
    assert "API key is not set" in mock_table.vector_index["last_error"]


def test_sync_table_vector_index_success_uses_the_active_provider(tmp_path: Path):
    import duckdb

    mock_table = _movies_table(_movies_parquet(tmp_path))
    mock_db = MagicMock()
    provider = FakeProvider(dimension=4)
    out_path = tmp_path / "out_vector.parquet"
    with patch("vector_index_service.get_active_provider", return_value=provider):
        res = vector_index_service.sync_table_vector_index(
            mock_table, mock_db, dest_s3=str(out_path)
        )
    assert res["ok"] is True
    assert res["status"] == "synced"
    assert res["total_vectors"] == 1
    assert "last_synced_at" in res
    assert mock_db.commit.called
    assert provider.calls == [["Movie: Inception | Overview: Dream thief"]]
    row = (
        duckdb.connect()
        .execute(
            f"SELECT provider, model, dimension, len(embedding) FROM read_parquet('{out_path}')"
        )
        .fetchone()
    )
    assert row == ("fake", "fake-embed", 4, 4)
    assert mock_table.vector_index["provider"] == "fake"
    assert mock_table.vector_index["model"] == "fake-embed"
    assert mock_table.vector_index["dimension"] == 4


def test_sync_table_vector_index_default_dest_s3_overwrites_static_path(tmp_path: Path):
    mock_table = _movies_table(_movies_parquet(tmp_path))
    mock_table.slug = "movies_test"
    mock_db = MagicMock()
    provider = FakeProvider(dimension=4)

    with (
        patch("vector_index_service.get_active_provider", return_value=provider),
        patch("vector_index_service.create_duckdb_connection") as mock_con_factory,
    ):
        mock_con = MagicMock()
        mock_con_factory.return_value = mock_con
        mock_con.execute.return_value.fetchall.side_effect = [
            [("overview", "VARCHAR"), ("id", "VARCHAR")],
            [("Dream thief", "1")],
        ]
        res = vector_index_service.sync_table_vector_index(mock_table, mock_db)

    assert res["ok"] is True
    assert res["s3_vector_path"] == "s3://datalake/embeddings/movies_test/data.parquet"
    assert (
        mock_table.vector_index["s3_vector_path"]
        == "s3://datalake/embeddings/movies_test/data.parquet"
    )


def test_sync_table_vector_index_is_not_tied_to_768_dimensions(tmp_path: Path):
    import duckdb

    out_path = tmp_path / "out_1536.parquet"
    provider = FakeProvider(dimension=1536, embedding_model="text-embedding-3-small")
    res = vector_index_service.sync_table_vector_index(
        _movies_table(_movies_parquet(tmp_path)),
        MagicMock(),
        dest_s3=str(out_path),
        provider=provider,
    )
    assert res["ok"] is True
    assert duckdb.connect().execute(
        f"SELECT len(embedding), dimension FROM read_parquet('{out_path}')"
    ).fetchone() == (1536, 1536)


def test_sync_table_vector_index_surfaces_provider_errors(tmp_path: Path):
    from ai_providers import ProviderError

    mock_table = _movies_table(_movies_parquet(tmp_path))
    mock_db = MagicMock()
    provider = FakeProvider()
    provider.embed_texts = MagicMock(side_effect=ProviderError("Ollama returned HTTP 404"))
    res = vector_index_service.sync_table_vector_index(
        mock_table,
        mock_db,
        dest_s3=str(tmp_path / "o.parquet"),
        provider=provider,
    )
    assert res == {"ok": False, "error": "Ollama returned HTTP 404"}
    assert mock_table.vector_index["status"] == "error"
    assert mock_table.vector_index["last_error"] == "Ollama returned HTTP 404"


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
