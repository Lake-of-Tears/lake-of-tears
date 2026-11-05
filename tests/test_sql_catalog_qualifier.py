"""Catalog-qualified names (catalog.schema.table) in the SQL Editor."""

import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("backend", "ui"):
    sys.path.insert(0, str(ROOT / sub))

CATALOGS = [{"name": "default", "slug": "default", "schemas": [{"slug": "telemetry"}]}]


@pytest.mark.parametrize("modname", ["ai_service", "duckdb_ai"])
def test_strips_known_catalog_qualifier(modname):
    mod = __import__(modname)
    out = mod.strip_catalog_qualifiers('CREATE TABLE "default".telemetry.events (a INT)', CATALOGS)
    assert "default" not in out.lower()
    con = duckdb.connect()
    con.execute("CREATE SCHEMA telemetry")
    con.execute(out)
    assert con.execute("SELECT count(*) FROM telemetry.events").fetchone() == (0,)


@pytest.mark.parametrize("modname", ["ai_service", "duckdb_ai"])
def test_leaves_other_sql_untouched(modname):
    mod = __import__(modname)
    for sql in (
        "SELECT * FROM telemetry.events",
        "SELECT * FROM memory.main.t",
        "SELECT * FROM other.telemetry.events",
        "SELECT 'default.a.b'",
    ):
        assert mod.strip_catalog_qualifiers(sql, CATALOGS) == sql
