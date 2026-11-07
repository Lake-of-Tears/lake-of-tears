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
    "ui_main_alpine_settings_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_alpine_settings_test"] = ui_main
ui_spec.loader.exec_module(ui_main)


def test_alpine_phase2_controller_files_exist():
    ctrl_dir = repo_root / "ui" / "static" / "js" / "controllers"
    admin_js = ctrl_dir / "admin.js"
    workspace_secrets_js = ctrl_dir / "workspace-secrets.js"

    assert admin_js.exists(), "admin.js controller must exist"
    assert workspace_secrets_js.exists(), "workspace-secrets.js controller must exist"

    admin_content = admin_js.read_text(encoding="utf-8")
    assert "adminTabsManager" in admin_content
    assert "servicesManager" in admin_content
    assert "aiModelDiscoveryManager" in admin_content
    assert "window.switchAdminTab" in admin_content
    assert "window.openServiceDrawer" in admin_content
    assert "window.closeServiceDrawer" in admin_content

    ws_content = workspace_secrets_js.read_text(encoding="utf-8")
    assert "workspaceSecretsManager" in ws_content
    assert "window.openCreateScopeModal" in ws_content
    assert "window.openKeysModal" in ws_content
    assert "window.openAclModal" in ws_content


def test_alpine_phase2_static_controllers_served():
    client = TestClient(ui_main.app)

    resp_admin = client.get("/static/js/controllers/admin.js")
    assert resp_admin.status_code == 200
    assert "adminTabsManager" in resp_admin.text
    assert "servicesManager" in resp_admin.text

    resp_ws = client.get("/static/js/controllers/workspace-secrets.js")
    assert resp_ws.status_code == 200
    assert "workspaceSecretsManager" in resp_ws.text


def test_admin_template_alpine_declarative_bindings():
    admin_html = (repo_root / "ui" / "templates" / "settings" / "admin.html").read_text(encoding="utf-8")

    # Script tag inclusion
    assert 'src="/static/js/controllers/admin.js"' in admin_html

    # Declarative component bindings
    assert 'x-data="adminTabsManager()"' in admin_html
    assert 'x-data="servicesManager(' in admin_html
    assert ":class=\"tabClass('services')\"" in admin_html
    assert ":class=\"tabClass('users')\"" in admin_html
    assert ":class=\"tabClass('workspaces')\"" in admin_html
    assert ":class=\"tabClass('vaults')\"" in admin_html
    assert ":class=\"tabClass('platform')\"" in admin_html
    assert 'id="services-autopoll-toggle"' in admin_html
    assert '@change="toggleAutoPoll(' in admin_html


def test_workspace_template_alpine_declarative_bindings():
    workspace_html = (repo_root / "ui" / "templates" / "settings" / "workspace.html").read_text(encoding="utf-8")

    # Script tag inclusion
    assert 'src="/static/js/controllers/workspace-secrets.js"' in workspace_html

    # Declarative component bindings
    assert 'x-data="workspaceSecretsManager(' in workspace_html
    assert ":class=\"{ 'hidden': !createScope.isOpen }\"" in workspace_html
    assert ":class=\"{ 'hidden': !keysDrawer.isOpen }\"" in workspace_html
    assert ":class=\"{ 'hidden': !aclModal.isOpen }\"" in workspace_html
