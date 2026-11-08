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
    "ui_main_alpine_catalog_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_alpine_catalog_test"] = ui_main
ui_spec.loader.exec_module(ui_main)


def test_alpine_phase3_controller_file_exists():
    ctrl_dir = repo_root / "ui" / "static" / "js" / "controllers"
    catalog_js = ctrl_dir / "catalog.js"

    assert catalog_js.exists(), "catalog.js controller must exist"

    content = catalog_js.read_text(encoding="utf-8")
    assert "trashDrawerManager" in content
    assert "tableDetailsDrawerManager" in content
    assert "catalogWizardsManager" in content
    assert "catalogReorderManager" in content

    # Bridge functions for test compatibility
    assert "window.openTrashDrawer" in content
    assert "window.closeTrashDrawer" in content
    assert "window.initTrashBadge" in content
    assert "window.restoreTrashAsset" in content
    assert "window.purgeTrashAsset" in content
    assert "window.showTable" in content
    assert "window.closePanel" in content
    assert "window.toggleMaximizePanel" in content
    assert "window.switchDrawerTab" in content
    assert "window.triggerVectorSync" in content
    assert "window.saveVectorConfig" in content
    assert "window.enrichCurrentTableAi" in content
    assert "window.openCreateSchemaModal" in content
    assert "window.openCreateTableUploadModal" in content
    assert "window.openCreateTablePromptModal" in content
    assert "window.openExportToonModal" in content
    assert "window.openDeleteTableModal" in content
    assert "window.openDeleteSchemaModal" in content
    assert "window.startCatalogDescEdit" in content
    assert "window.saveCatalogDesc" in content
    assert "window.cancelCatalogDescEdit" in content
    assert "window.persistCatalogOrder" in content
    assert "window.initCatalogDragAndDrop" in content


def test_alpine_phase3_static_controller_served():
    client = TestClient(ui_main.app)

    resp_cat = client.get("/static/js/controllers/catalog.js")
    assert resp_cat.status_code == 200
    assert "trashDrawerManager" in resp_cat.text
    assert "tableDetailsDrawerManager" in resp_cat.text
    assert "catalogWizardsManager" in resp_cat.text
    assert "catalogReorderManager" in resp_cat.text


def test_catalog_template_alpine_declarative_bindings():
    catalog_html = (repo_root / "ui" / "templates" / "catalog.html").read_text(encoding="utf-8")

    # Script tag inclusion
    assert 'src="/static/js/controllers/catalog.js"' in catalog_html

    # Declarative component bindings
    assert 'x-data="trashDrawerManager()"' in catalog_html
    assert 'x-data="tableDetailsDrawerManager()"' in catalog_html
    assert 'x-data="catalogWizardsManager()"' in catalog_html
    assert 'x-data="catalogReorderManager(' in catalog_html

    # Elements & container IDs preserved
    assert 'id="trash-drawer"' in catalog_html
    assert 'id="table-panel"' in catalog_html
    assert 'id="catalog-wizards-container"' in catalog_html
    assert 'id="catalog-trash-container"' in catalog_html


def test_ai_enrich_endpoints_mounted_and_controller_updated():
    client = TestClient(ui_main.app)

    # Check endpoints exist (not 404)
    resp = client.post("/api/catalog/ai/generate-description", json={})
    assert resp.status_code != 404

    resp = client.post("/api/ai/generate-description", json={})
    assert resp.status_code != 404

    resp = client.post("/api/catalog/ai/enrich-table", json={})
    assert resp.status_code != 404

    resp = client.post("/api/ai/enrich-table", json={})
    assert resp.status_code != 404

    # Verify catalog.js references valid endpoints and resolves table_name
    catalog_js = (repo_root / "ui" / "static" / "js" / "controllers" / "catalog.js").read_text(
        encoding="utf-8"
    )
    assert "/api/catalog/ai/generate-description" in catalog_js
    assert "/api/ai/generate-description" in catalog_js
    assert "name: tableName" in catalog_js
    assert "table_name: tableName" in catalog_js


def test_ai_semantic_description_generation_and_schema_compatibility():
    import ai_service
    from schemas import GenerateDescriptionRequest

    # Verify GenerateDescriptionRequest accepts table_name without error
    req = GenerateDescriptionRequest(
        asset_type="table",
        table_name="pubmed_articles",
        catalog_name="default",
        schema_name="bronze",
        columns=[{"name": "pmid", "type": "VARCHAR"}, {"name": "title", "type": "VARCHAR"}],
        sample_rows=[{"pmid": "12345", "title": "Example Study"}],
    )
    assert req.name == "pubmed_articles"
    assert req.table_name == "pubmed_articles"

    # Test heuristic fallback generation
    res = ai_service.generate_semantic_description(
        asset_type=req.asset_type,
        name=req.name,
        catalog_name=req.catalog_name,
        schema_name=req.schema_name,
        columns=req.columns,
        sample_rows=req.sample_rows,
    )
    assert res["ok"] is True
    assert (
        "pubmed" in res["description"].lower()
        or "dataset" in res["description"].lower()
        or "table" in res["description"].lower()
    )
    assert "pmid" in res["column_descriptions"]
    assert "title" in res["column_descriptions"]
