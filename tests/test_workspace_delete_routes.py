from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

spec = importlib.util.spec_from_file_location(
    "backend_main_ws_delete", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend_main)


def _delete_paths() -> set[str]:
    return {
        r.path
        for r in backend_main.app.routes
        if "DELETE" in (getattr(r, "methods", None) or set())
    }


def test_legacy_unconfirmed_workspace_delete_route_is_removed():
    assert "/api/workspaces/{workspace_id}" not in _delete_paths()


def test_workspace_deletion_goes_through_the_admin_route_only():
    paths = {
        p for p in _delete_paths() if p.startswith(("/api/workspaces/", "/api/admin/workspaces"))
    }
    assert "/api/admin/workspaces/{workspace_id}" in paths
    assert not any(p.count("/") == 3 and p.startswith("/api/workspaces/") for p in paths), (
        "a bare DELETE /api/workspaces/{id} route would bypass the admin safeguards"
    )
