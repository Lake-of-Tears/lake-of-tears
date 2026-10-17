"""Catalog slugs use hyphens, DuckDB views use underscores: the SQL Editor must show the latter."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ui_dir = str(Path(__file__).parents[1] / "ui")
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

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


def test_the_view_exists_under_the_underscore_name_only():
    con = duckdb.connect()
    # register_catalog_views swallows read errors, so point at a real file
    con.execute("COPY (SELECT 1 AS x) TO '/tmp/_tmdb_names_test.parquet' (FORMAT parquet)")
    catalogs = [
        {
            "slug": "default",
            "schemas": [
                {
                    "slug": "media",
                    "tables": [
                        {
                            "slug": "tmdb-trending",
                            "s3_path_pattern": "/tmp/_tmdb_names_test.parquet",
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
