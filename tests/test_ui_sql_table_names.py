"""Catalog slugs use hyphens, DuckDB views use underscores: the SQL Editor must show the latter."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ui_dir = str(Path(__file__).parents[1] / "ui")
backend_dir = str(Path(__file__).parents[1] / "backend")
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import ai_service  # noqa: E402
import duckdb_ai  # noqa: E402
import main  # noqa: E402

CATALOGS = [
    {
        "slug": "default",
        "schemas": [
            {
                "slug": "media",
                "tables": [
                    {"slug": "tmdb-trending", "s3_path_pattern": "s3://datalake/raw/tmdb/*.parquet"}
                ],
            }
        ],
    }
]


def test_template_filter_gives_the_name_the_view_is_registered_under():
    render = main.templates.env.filters["sql_ident"]
    assert render("tmdb-trending") == "tmdb_trending"
    assert render("charges") == "charges"
    assert render(None) == "t_"
    assert render("") == "t_"


def test_the_view_exists_under_the_underscore_name_only(tmp_path: Path):
    con = duckdb.connect()
    parquet_path = tmp_path / "_tmdb_names_test.parquet"
    # register_catalog_views swallows read errors, so point at a real file
    con.execute(f"COPY (SELECT 1 AS x) TO '{parquet_path}' (FORMAT parquet)")
    catalogs = [
        {
            "slug": "default",
            "schemas": [
                {
                    "slug": "media",
                    "tables": [
                        {
                            "slug": "tmdb-trending",
                            "s3_path_pattern": str(parquet_path),
                        }
                    ],
                }
            ],
        }
    ]
    assert duckdb_ai.register_catalog_views(con, catalogs) == ["media.tmdb_trending"]
    assert con.execute("SELECT x FROM media.tmdb_trending").fetchall() == [(1,)]


def test_ai_prompt_context_names_the_queryable_view():
    text = duckdb_ai.build_schema_prompt_context(CATALOGS)
    assert "View: `media.tmdb_trending`" in text
    assert "media.tmdb-trending`" not in text.split("View:")[1].split("\n")[0]


def test_backend_ai_prompt_context_names_the_queryable_view():
    text = ai_service.build_schema_prompt_context(CATALOGS)
    assert "View: `media.tmdb_trending`" in text
    assert "media.tmdb-trending`" not in text.split("View:")[1].split("\n")[0]


def test_catalog_page_renders_sql_ident_for_schema_and_table():
    from unittest.mock import patch

    import jwt
    from starlette.testclient import TestClient

    client = TestClient(main.app)
    token = jwt.encode(
        {
            "sub": "00000000-0000-0000-0000-000000000001",
            "email": "test@lake.local",
            "role": "admin",
        },
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]
    mock_catalogs = [
        {
            "id": "cat-1",
            "slug": "default",
            "name": "Default Catalog",
            "schemas": [
                {
                    "id": "sch-1",
                    "slug": "media-bronze",
                    "name": "Media Bronze",
                    "tier": "bronze",
                    "tables": [
                        {
                            "id": "tbl-1",
                            "slug": "tmdb-trending",
                            "name": "TMDB Trending Movies",
                            "s3_path_pattern": "s3://datalake/raw/tmdb/*.parquet",
                        }
                    ],
                }
            ],
        }
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=mock_catalogs),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        # Schema displays SQL identifier and secondary title
        assert "media_bronze" in res.text
        assert "(Media Bronze)" in res.text
        # Table displays SQL identifier and secondary title
        assert "tmdb_trending" in res.text
        # Data attributes pass sanitized SQL identifiers to openTablePanel
        assert 'data-cat="default"' in res.text
        assert 'data-sch="media_bronze"' in res.text
        assert 'data-table-ident="tmdb_trending"' in res.text
        assert "openTablePanel(this)" in res.text
