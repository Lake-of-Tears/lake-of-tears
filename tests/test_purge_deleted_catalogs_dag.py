from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

DAG_PATH = Path(__file__).parents[1] / "pipeline" / "dags" / "purge_deleted_catalogs_dag.py"


@pytest.fixture
def dag(monkeypatch):
    """Load the DAG module with airflow stubbed (it is only importable in the Airflow image)."""
    airflow = types.ModuleType("airflow")
    airflow.DAG = MagicMock()
    operators = types.ModuleType("airflow.operators")
    python = types.ModuleType("airflow.operators.python")
    python.PythonOperator = MagicMock()
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.operators", operators)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", python)
    spec = importlib.util.spec_from_file_location("purge_deleted_catalogs_dag", DAG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_purge(dag, monkeypatch, rows_due, patterns):
    """Run purge_deleted_catalogs against a fake DB; return the prefixes it deleted."""
    deleted: list[str] = []
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows_due
    sa = types.ModuleType("sqlalchemy")
    sa.create_engine = lambda *a, **k: MagicMock()
    sa.text = lambda q: q
    orm = types.ModuleType("sqlalchemy.orm")
    orm.sessionmaker = lambda **k: (lambda: db)
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa)
    monkeypatch.setitem(sys.modules, "sqlalchemy.orm", orm)
    monkeypatch.setattr(dag, "_load_patterns", lambda _db: patterns)
    monkeypatch.setattr(dag, "_delete_s3_prefix", deleted.append)
    dag.purge_deleted_catalogs()
    return deleted


def test_prefix_derivation(dag):
    assert dag._prefix_for_pattern("s3://datalake/a/b/**/*.parquet") == "a/b/"
    assert dag._prefix_for_pattern("s3://datalake/*.parquet") is None


def test_overlap_rule_is_symmetric(dag):
    assert dag._overlaps("raw/charges/", "raw/")
    assert dag._overlaps("raw/", "raw/charges/")
    assert not dag._overlaps("raw/charges/", "raw/orders/")


def test_purge_spares_prefix_shared_with_a_surviving_catalog(dag, monkeypatch):
    patterns = [
        ("A", "s3://datalake/mine/t1/**/*.parquet"),
        ("A", "s3://datalake/raw/charges/**/*.parquet"),
        ("B", "s3://datalake/raw/charges/year=*/*.parquet"),
    ]
    assert _run_purge(dag, monkeypatch, [("A", "a")], patterns) == ["mine/t1/"]


def test_purge_deletes_prefix_shared_only_by_catalogs_purged_together(dag, monkeypatch):
    patterns = [
        ("A", "s3://datalake/raw/charges/**/*.parquet"),
        ("B", "s3://datalake/raw/charges/**/*.parquet"),
    ]
    deleted = _run_purge(dag, monkeypatch, [("A", "a"), ("B", "b")], patterns)
    assert deleted == ["raw/charges/", "raw/charges/"]
