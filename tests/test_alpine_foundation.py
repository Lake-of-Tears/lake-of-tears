from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from starlette.testclient import TestClient

repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
ui_dir = str(repo_root / "ui")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

ui_spec = importlib.util.spec_from_file_location(
    "ui_main_alpine_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_alpine_test"] = ui_main
ui_spec.loader.exec_module(ui_main)


def test_alpine_vendor_assets_exist():
    vendor_dir = repo_root / "ui" / "static" / "vendor"
    alpine_js = vendor_dir / "alpine.min.js"
    collapse_js = vendor_dir / "alpine-collapse.min.js"

    assert alpine_js.exists(), "alpine.min.js must be vendored for air-gapped deployments"
    assert (
        collapse_js.exists()
    ), "alpine-collapse.min.js must be vendored for air-gapped deployments"
    assert alpine_js.stat().st_size > 10000, "alpine.min.js must not be empty"
    assert collapse_js.stat().st_size > 500, "alpine-collapse.min.js must not be empty"


def test_alpine_inactivity_controller_exists():
    ctrl_path = repo_root / "ui" / "static" / "js" / "controllers" / "inactivity.js"
    assert ctrl_path.exists(), "inactivity.js controller must exist"
    content = ctrl_path.read_text(encoding="utf-8")
    assert "Alpine.data('inactivityManager'" in content
    assert "fmtCountdown" in content
    assert "showWarning" in content
    assert "stayLoggedIn" in content
    assert "signOutNow" in content


def test_base_template_alpine_integration():
    base_html = (repo_root / "ui" / "templates" / "base.html").read_text(encoding="utf-8")

    # Script tag inclusions
    assert 'src="/static/vendor/alpine-collapse.min.js"' in base_html
    assert 'src="/static/vendor/alpine.min.js"' in base_html
    assert 'src="/static/js/controllers/inactivity.js"' in base_html
    assert "[x-cloak] { display: none !important; }" in base_html

    # Declarative component bindings
    assert 'id="ws-switcher-wrap" x-data="{ open: false }"' in base_html
    assert 'id="user-menu-wrap" x-data="{ open: false }"' in base_html
    assert 'x-data="inactivityManager(' in base_html
    assert ":class=\"{ 'hidden': !warningActive }\"" in base_html


def test_static_vendor_assets_served_by_app():
    client = TestClient(ui_main.app)
    resp_alpine = client.get("/static/vendor/alpine.min.js")
    assert resp_alpine.status_code == 200
    assert "Alpine" in resp_alpine.text or "alpine" in resp_alpine.text

    resp_collapse = client.get("/static/vendor/alpine-collapse.min.js")
    assert resp_collapse.status_code == 200

    resp_ctrl = client.get("/static/js/controllers/inactivity.js")
    assert resp_ctrl.status_code == 200
    assert "inactivityManager" in resp_ctrl.text
