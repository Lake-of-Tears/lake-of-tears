"""DuckDB connection pool: reuse, correct counting under errors and threads, and cleanup between users."""

from __future__ import annotations

import threading
import time

import duckdb
import duckdb_pool
import pytest
from duckdb_pool import DuckDBConnectionPool, get_s3_duckdb_config


def plain_pool(max_size: int = 2, factory=None):
    made: list[duckdb.DuckDBPyConnection] = []

    def make():
        con = duckdb.connect()
        made.append(con)
        return con

    pool = DuckDBConnectionPool(max_size=max_size, connection_factory=factory or make)
    return pool, made


# ── reuse and counting ────────────────────────────────────────────────────────


def test_a_connection_is_reused_across_requests():
    pool, made = plain_pool()
    with pool.acquire() as first:
        pass
    with pool.acquire() as second:
        pass
    assert first is second
    assert len(made) == 1


def test_object_cache_is_enabled_by_default():
    assert get_s3_duckdb_config()["enable_object_cache"] is True


def test_failed_queries_do_not_shrink_the_pool():
    """A typo in the SQL Editor is not a broken connection: it must keep being reused."""
    pool, made = plain_pool(max_size=2)
    for _ in range(5):
        with pytest.raises(duckdb.Error):
            with pool.acquire(timeout=0.2) as con:
                con.execute("SELECT * FROM table_that_does_not_exist")
    started = time.perf_counter()
    with pool.acquire(timeout=0.2) as con:
        assert con.execute("SELECT 1").fetchone() == (1,)
    assert time.perf_counter() - started < 0.15  # no waiting for a timeout
    assert len(made) == 1
    assert pool._created_count == 1


def test_a_closed_connection_is_replaced_and_the_count_stays_right():
    pool, made = plain_pool(max_size=2)
    with pool.acquire() as con:
        pass
    con.close()  # the pooled connection dies while idle
    with pool.acquire() as replacement:
        assert replacement.execute("SELECT 2").fetchone() == (2,)
    assert replacement is not con
    assert pool._created_count == 1
    assert len(made) == 2


def test_a_failing_factory_does_not_leak_a_slot():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("cannot connect")
        return duckdb.connect()

    pool = DuckDBConnectionPool(max_size=1, connection_factory=flaky)
    with pytest.raises(RuntimeError):
        with pool.acquire(timeout=0.2):
            pass
    assert pool._created_count == 0
    with pool.acquire(timeout=0.2) as con:  # the single slot is still available
        assert con.execute("SELECT 1").fetchone() == (1,)


def test_the_pool_never_creates_more_connections_than_max_size_under_threads():
    made: list[duckdb.DuckDBPyConnection] = []
    lock = threading.Lock()

    def slow_factory():
        time.sleep(0.05)  # widens the race that an unlocked counter would lose
        con = duckdb.connect()
        with lock:
            made.append(con)
        return con

    pool = DuckDBConnectionPool(max_size=2, connection_factory=slow_factory)
    errors: list[Exception] = []

    def work():
        try:
            with pool.acquire(timeout=5) as con:
                time.sleep(0.05)
                con.execute("SELECT 1").fetchall()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(made) == 2
    assert pool._created_count == 2


def test_prewarm_and_close_all():
    pool, made = plain_pool(max_size=3)
    pool.prewarm(2)
    assert pool._created_count == 2 and len(made) == 2
    pool.close_all()
    assert pool._created_count == 0 and pool._pool.empty()


# ── cleanup between users ─────────────────────────────────────────────────────


def objects_left(con) -> dict[str, list]:
    q = con.execute
    return {
        "schemas": q(
            "select schema_name from duckdb_schemas() where not internal and schema_name <> 'main'"
        ).fetchall(),
        "tables": q("select table_name from duckdb_tables() where not internal").fetchall(),
        "views": q("select view_name from duckdb_views() where not internal").fetchall(),
        "macros": q(
            "select function_name from duckdb_functions() "
            "where not internal and function_type in ('macro', 'table_macro')"
        ).fetchall(),
        "sequences": q("select sequence_name from duckdb_sequences()").fetchall(),
        "types": q("select type_name from duckdb_types() where not internal").fetchall(),
    }


def test_objects_created_by_one_request_are_gone_for_the_next():
    pool, made = plain_pool(max_size=1)
    with pool.acquire() as con:
        con.execute("CREATE SCHEMA media")
        con.execute("CREATE VIEW media.tmdb AS SELECT 1 AS a")
        con.execute("CREATE TABLE main.secret_stuff AS SELECT 'private' AS v")
        con.execute("CREATE TEMP TABLE scratch AS SELECT 2 AS b")
        con.execute("CREATE VIEW main.v AS SELECT 3 AS c")
        con.execute("CREATE MACRO add_one(x) AS x + 1")
        con.execute("CREATE MACRO rows_of(n) AS TABLE SELECT * FROM range(n)")
        con.execute("CREATE SEQUENCE ids")
        con.execute("CREATE TYPE mood AS ENUM ('a', 'b')")
        con.execute("USE media")  # even the current schema may be dropped
        assert any(objects_left(con).values())
    with pool.acquire() as con:
        assert len(made) == 1  # same connection, now clean
        assert not any(objects_left(con).values()), objects_left(con)
        assert con.execute("SELECT current_schema()").fetchone() == ("main",)
        with pytest.raises(duckdb.CatalogException):
            con.execute("SELECT * FROM media.tmdb")
        with pytest.raises(duckdb.CatalogException):
            con.execute("SELECT * FROM secret_stuff")


def test_attached_databases_are_detached(tmp_path):
    pool, made = plain_pool(max_size=1)
    with pool.acquire() as con:
        con.execute(f"ATTACH '{tmp_path / 'other.duckdb'}' AS other")
        con.execute("CREATE TABLE other.t AS SELECT 1 AS a")
    with pool.acquire() as con:
        names = [
            r[0] for r in con.execute("select database_name from duckdb_databases()").fetchall()
        ]
        assert "other" not in names
        assert len(made) == 1


def test_changed_settings_are_restored():
    pool, _ = plain_pool(max_size=1)
    names = ("threads", "default_null_order")
    with pool.acquire() as con:
        original = {n: con.execute(f"SELECT current_setting('{n}')").fetchone()[0] for n in names}
        con.execute("SET threads = 1")
        con.execute("SET default_null_order = 'nulls_first'")
        changed = {n: con.execute(f"SELECT current_setting('{n}')").fetchone()[0] for n in names}
        assert changed != original
    with pool.acquire() as con:
        restored = {n: con.execute(f"SELECT current_setting('{n}')").fetchone()[0] for n in names}
        assert restored == original


def test_secrets_created_by_a_request_are_dropped():
    pool, _ = plain_pool(max_size=1)
    with pool.acquire() as con:
        con.execute("CREATE SECRET leaked (TYPE s3, KEY_ID 'k', SECRET 's')")
        assert con.execute("select count(*) from duckdb_secrets()").fetchone()[0] == 1
    with pool.acquire() as con:
        assert con.execute("select count(*) from duckdb_secrets()").fetchone()[0] == 0


def test_a_connection_that_cannot_be_cleaned_is_discarded_not_reused(monkeypatch):
    pool, made = plain_pool(max_size=1)
    with pool.acquire():
        pass

    def broken_reset(*args, **kwargs):
        raise RuntimeError("cannot reset")

    monkeypatch.setattr(duckdb_pool, "reset_connection_state", broken_reset)
    with pool.acquire() as con:
        con.execute("CREATE TABLE leftover AS SELECT 1")
    assert pool._created_count == 0  # discarded and its slot freed
    monkeypatch.undo()
    with pool.acquire() as con:
        assert len(made) == 2  # a new connection, without the leftover table
        assert (
            con.execute("select count(*) from duckdb_tables() where not internal").fetchone()[0]
            == 0
        )


def test_a_cancelled_query_leaves_a_usable_clean_connection():
    pool, made = plain_pool(max_size=1)
    result: dict[str, object] = {}

    def long_query(con):
        try:
            con.execute("CREATE TEMP TABLE t AS SELECT range FROM range(100000000)")
            con.execute("SELECT sum(a.range * b.range) FROM t a, t b")
        except Exception as exc:
            result["error"] = exc

    with pool.acquire() as con:
        worker = threading.Thread(target=long_query, args=(con,))
        worker.start()
        time.sleep(0.3)
        con.interrupt()
        worker.join(timeout=20)
    with pool.acquire() as con:
        assert con.execute("SELECT 42").fetchone() == (42,)
        assert not any(objects_left(con).values())
    assert len(made) == 1


# ── the macro sweep is only needed when the SQL could define one ──────────────


def _record_sweeps(monkeypatch):
    seen: list[bool] = []
    real = duckdb_pool.reset_connection_state

    def spy(con, settings=None, secrets=None, sweep_macros=True):
        seen.append(sweep_macros)
        return real(con, settings, secrets, sweep_macros)

    monkeypatch.setattr(duckdb_pool, "reset_connection_state", spy)
    return seen


@pytest.mark.parametrize(
    ("sql", "sweeps"),
    [
        ("SELECT 1", False),
        ("select * from media.tmdb_trending where a = 'x'", False),
        ("CREATE MACRO m(x) AS x + 1", True),
        ("create or replace temp macro m(x) as x", True),
        ("CREATE FUNCTION f(x) AS x", True),
        ("SELECT 1; /* sneaky */ CREATE   MACRO m() AS 1", True),
        (None, True),  # no hint: always sweep
    ],
)
def test_the_macro_sweep_runs_only_when_the_sql_could_define_one(monkeypatch, sql, sweeps):
    seen = _record_sweeps(monkeypatch)
    pool, _ = plain_pool(max_size=1)
    with pool.acquire(sql=sql):
        pass
    assert seen == [sweeps]


def test_a_macro_created_by_hinted_sql_is_removed():
    pool, made = plain_pool(max_size=1)
    sql = "CREATE MACRO leaked(x) AS x * 2"
    with pool.acquire(sql=sql) as con:
        con.execute(sql)
    with pool.acquire(sql="SELECT 1") as con:
        assert len(made) == 1
        assert objects_left(con)["macros"] == []
