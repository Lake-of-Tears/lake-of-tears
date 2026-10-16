from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from models import Workspace  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_default_ws", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend_main)


def test_default_workspace_is_created_as_system_workspace():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    user = MagicMock()
    with patch.object(backend_main, "_create_default_catalog"):
        ws = backend_main._create_default_workspace(db, user)
    assert isinstance(ws, Workspace)
    assert ws.is_system is True
    assert ws.name == "Default"
