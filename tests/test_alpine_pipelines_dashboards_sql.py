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
    "ui_main_alpine_phase4_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_alpine_phase4_test"] = ui_main
ui_spec.loader.exec_module(ui_main)


def test_alpine_phase4_controller_files_exist():
    ctrl_dir = repo_root / "ui" / "static" / "js" / "controllers"
    pipelines_js = ctrl_dir / "pipelines.js"
    dashboards_js = ctrl_dir / "dashboards.js"
    sql_js = ctrl_dir / "sql.js"

    assert pipelines_js.exists(), "pipelines.js controller must exist"
    assert dashboards_js.exists(), "dashboards.js controller must exist"
    assert sql_js.exists(), "sql.js controller must exist"

    p_content = pipelines_js.read_text(encoding="utf-8")
    assert "pipelinesManager" in p_content
    assert "window.openInspector" in p_content
    assert "window.closeInspector" in p_content
    assert "window.openTriggerModal" in p_content
    assert "window.closeTriggerModal" in p_content
    assert "window.openDeletePipelineModal" in p_content
    assert "window.closeDeletePipelineModal" in p_content
    assert "window.startLogPolling" in p_content
    assert "window.stopLogPolling" in p_content

    d_content = dashboards_js.read_text(encoding="utf-8")
    assert "dashboardsGalleryManager" in d_content
    assert "window.openCreateModal" in d_content
    assert "window.closeCreateModal" in d_content
    assert "window.confirmDelete" in d_content
    assert "window.closeDeleteModal" in d_content

    s_content = sql_js.read_text(encoding="utf-8")
    assert "sqlEditorManager" in s_content
    assert "window.toggleSqlSidebar" in s_content
    assert "window.switchSidebarTab" in s_content
    assert "window.executeRunQuery" in s_content
    assert "window.triggerFormatSql" in s_content
    assert "window.explainCurrentQuery" in s_content
    assert "window.closeExplainModal" in s_content


def test_alpine_phase4_static_controllers_served():
    client = TestClient(ui_main.app)

    resp_p = client.get("/static/js/controllers/pipelines.js")
    assert resp_p.status_code == 200
    assert "pipelinesManager" in resp_p.text

    resp_d = client.get("/static/js/controllers/dashboards.js")
    assert resp_d.status_code == 200
    assert "dashboardsGalleryManager" in resp_d.text

    resp_s = client.get("/static/js/controllers/sql.js")
    assert resp_s.status_code == 200
    assert "sqlEditorManager" in resp_s.text


def test_pipelines_template_alpine_declarative_bindings():
    pipelines_html = (repo_root / "ui" / "templates" / "pipelines.html").read_text(encoding="utf-8")

    assert 'src="/static/js/controllers/pipelines.js"' in pipelines_html
    assert 'x-data="pipelinesManager()"' in pipelines_html
    assert 'id="pipelines-container"' in pipelines_html
    assert 'id="inspector-drawer"' in pipelines_html
    assert 'id="trigger-modal"' in pipelines_html
    assert 'id="pipeline-delete-modal"' in pipelines_html


def test_dashboards_template_alpine_declarative_bindings():
    dashboards_html = (repo_root / "ui" / "templates" / "dashboards.html").read_text(
        encoding="utf-8"
    )

    assert 'src="/static/js/controllers/dashboards.js"' in dashboards_html
    assert 'x-data="dashboardsGalleryManager()"' in dashboards_html
    assert 'id="dashboards-gallery"' in dashboards_html
    assert 'id="create-modal"' in dashboards_html
    assert 'id="delete-modal"' in dashboards_html


def test_sql_template_alpine_declarative_bindings():
    sql_html = (repo_root / "ui" / "templates" / "sql.html").read_text(encoding="utf-8")

    assert 'src="/static/js/controllers/sql.js"' in sql_html
    assert 'x-data="sqlEditorManager()"' in sql_html
    assert 'id="toggleSidebarBtn"' in sql_html


def test_pipelines_drawer_header_alignment_and_controls():
    pipelines_html = (repo_root / "ui" / "templates" / "pipelines.html").read_text(encoding="utf-8")

    # Header layout and spacing
    assert 'id="inspector-drawer-header"' in pipelines_html
    assert "gap-4" in pipelines_html
    assert "min-w-0" in pipelines_html

    # Hierarchy: Display name input is primary with status badge, dag_id is secondary with truncation
    assert 'id="drawer-name-input"' in pipelines_html
    assert 'id="drawer-status-badge"' in pipelines_html
    assert 'id="drawer-dag-id"' in pipelines_html
    assert "truncate max-w-[240px]" in pipelines_html or "truncate" in pipelines_html

    # Drawer width responsiveness
    assert "lg:max-w-3xl" in pipelines_html or "max-w-2xl" in pipelines_html

    # Color swatch and action buttons
    assert 'id="drawer-color-swatch"' in pipelines_html
    assert 'id="drawer-run-split-btn"' in pipelines_html
    assert 'id="btn-delete-pipeline-drawer"' in pipelines_html
    assert 'id="btn-save-drawer-meta"' in pipelines_html


def test_pipelines_controller_full_form_and_drawer_actions():
    pipelines_js = (repo_root / "ui" / "static" / "js" / "controllers" / "pipelines.js").read_text(
        encoding="utf-8"
    )

    # Metadata saving
    assert "savePipelineMetadata" in pipelines_js
    assert "/settings" in pipelines_js
    assert "/schedule" in pipelines_js

    # Pause / unpause toggle uses PATCH to /api/pipelines/{dag_id}
    assert "togglePipelinePause" in pipelines_js
    assert "togglePipelinePauseFromDrawer" in pipelines_js
    assert "PATCH" in pipelines_js
    assert "is_paused" in pipelines_js

    # Runs and log polling
    assert "loadDrawerRuns" in pipelines_js
    assert "inspectRunTasks" in pipelines_js
    assert "cancelPipelineRun" in pipelines_js
    assert "activeDrawerTab" in pipelines_js
    assert "drawer-tab-runs" in pipelines_js
    assert "drawer-tab-logs" in pipelines_js
