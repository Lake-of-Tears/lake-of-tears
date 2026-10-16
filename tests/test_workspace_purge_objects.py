from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from models import Workspace  # noqa: E402

root = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("backend_main_ws_purge", root / "backend" / "main.py")
backend_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend_main)


def _db_with_rows(owned_ids, rows):
    db = MagicMock()
    q = db.query.return_value
    q.filter.return_value = [MagicMock(id=i) for i in owned_ids]
    q.join.return_value.filter.return_value.all.return_value = rows
    return db


def test_workspace_purge_deletes_owned_prefixes_and_spares_shared_ones():
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    rows = [
        ("s3://datalake/ws/a/t1/**/*.parquet", mine),
        ("s3://datalake/shared/raw/*.parquet", mine),
        ("s3://datalake/shared/raw/year=*/*.parquet", theirs),
    ]
    db = _db_with_rows([mine], rows)
    with patch.object(backend_main, "delete_prefix") as dp:
        backend_main._purge_workspace_objects(db, MagicMock())
    assert [c.args[0] for c in dp.call_args_list] == ["ws/a/t1/"]


def test_admin_hard_delete_returns_502_and_keeps_rows_when_storage_fails():
    ws = Workspace(name="analytics", slug="analytics", is_system=False)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = ws
    req = MagicMock(mode="hard", confirm_name="analytics")
    with (
        patch.object(backend_main, "_purge_workspace_objects", side_effect=RuntimeError("down")),
        pytest.raises(HTTPException) as exc,
    ):
        backend_main.admin_delete_workspace("id", req, db, MagicMock())
    assert exc.value.status_code == 502
    db.delete.assert_not_called()
    db.commit.assert_not_called()


def test_admin_hard_delete_purges_objects_before_deleting_rows():
    ws = Workspace(name="analytics", slug="analytics", is_system=False)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = ws
    req = MagicMock(mode="hard", confirm_name="analytics")
    order = []
    with patch.object(backend_main, "_purge_workspace_objects", lambda *_: order.append("s3")):
        db.delete.side_effect = lambda *_: order.append("db")
        backend_main.admin_delete_workspace("id", req, db, MagicMock())
    assert order == ["s3", "db"]
