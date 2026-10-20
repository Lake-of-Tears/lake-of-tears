"""The backend uses the psycopg (v3) driver for PostgreSQL, whatever form the URL is configured in."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from database import normalize_database_url  # noqa: E402


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("postgresql://u:p@postgres:5432/db", "postgresql+psycopg://u:p@postgres:5432/db"),
        ("postgres://u:p@postgres:5432/db", "postgresql+psycopg://u:p@postgres:5432/db"),
        (
            "postgresql+psycopg2://u:p@postgres:5432/db",
            "postgresql+psycopg://u:p@postgres:5432/db",
        ),
        (
            "postgresql+psycopg://u:p@postgres:5432/db",
            "postgresql+psycopg://u:p@postgres:5432/db",
        ),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
    ],
)
def test_postgres_urls_are_normalised_to_psycopg_v3(configured, expected):
    assert normalize_database_url(configured) == expected


def test_the_engine_really_uses_the_psycopg_v3_driver():
    from database import engine

    assert engine.dialect.driver == "psycopg"
