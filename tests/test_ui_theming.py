"""Tests for Epic #372: Standardized UI theming with semantic CSS tokens and dynamic editor/chart light theme support."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT_DIR / "ui" / "templates"
CONTROLLERS_DIR = ROOT_DIR / "ui" / "static" / "js" / "controllers"


def test_base_html_defines_semantic_css_tokens_for_dark_and_light_themes():
    """Phase 1 (#459): Verify all semantic CSS design tokens exist in :root and html[data-theme='light']."""
    base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

    required_tokens = [
        "--surface-page",
        "--surface-card",
        "--surface-elevated",
        "--surface-muted",
        "--surface-hover",
        "--text-primary",
        "--text-secondary",
        "--text-muted",
        "--text-inverse",
        "--border-subtle",
        "--border-default",
        "--border-strong",
        "--input-bg",
        "--input-border",
        "--input-focus",
        "--btn-ghost-hover",
    ]
    for token in required_tokens:
        # Each token must appear at least twice: once in :root (dark) and once in html[data-theme="light"]
        occurrences = base_html.count(f"{token}:")
        assert (
            occurrences >= 2
        ), f"Expected semantic token {token} in both dark and light scopes, found {occurrences}"


def test_base_html_dispatches_theme_change_event_and_exposes_helpers():
    """Phase 1 (#459): Verify getLakeTheme, getLakeEChartsThemeConfig, syncLakeEChartsTheme, and lake:theme-change."""
    base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

    assert "function getLakeTheme()" in base_html
    assert "window.getLakeTheme = getLakeTheme;" in base_html
    assert "function getLakeEChartsThemeConfig(isLight)" in base_html
    assert "function syncLakeEChartsTheme(isLight)" in base_html
    assert "new CustomEvent('lake:theme-change'" in base_html
    assert ":not(.keep-dark)" in base_html


def test_sql_editor_monaco_dynamic_light_and_dark_theme_sync():
    """Phase 2 (#460): Verify Monaco Editor defines lake-dark and lake-light and syncs on theme change."""
    sql_html = (TEMPLATES_DIR / "sql.html").read_text(encoding="utf-8")

    assert 'monaco.editor.defineTheme("lake-dark"' in sql_html
    assert 'monaco.editor.defineTheme("lake-light"' in sql_html
    assert "function resolveMonacoTheme()" in sql_html
    assert "function syncMonacoTheme()" in sql_html
    assert "monaco.editor.setTheme(resolveMonacoTheme())" in sql_html
    assert 'window.addEventListener("lake:theme-change", syncMonacoTheme)' in sql_html


def test_notebooks_codemirror_dynamic_light_and_dark_theme_sync():
    """Phase 2 (#460): Verify CodeMirror switches between default (light) and dracula (dark) dynamically."""
    notebooks_html = (TEMPLATES_DIR / "notebooks.html").read_text(encoding="utf-8")

    assert 'html[data-theme="light"] .CodeMirror' in notebooks_html
    assert 'html[data-theme="light"] .CodeMirror-gutters' in notebooks_html
    assert "function syncCodeMirrorTheme()" in notebooks_html
    assert "cm.setOption('theme', cmTheme)" in notebooks_html
    assert "window.addEventListener('lake:theme-change', syncCodeMirrorTheme)" in notebooks_html


def test_echarts_dynamic_theme_sync_across_dashboards_ai_and_lakeset():
    """Phase 2 (#460): Verify ECharts components use getLakeEChartsThemeConfig across dashboards, Genie AI, and Lakeset."""
    dashboard_view_html = (TEMPLATES_DIR / "dashboard_view.html").read_text(encoding="utf-8")
    ai_html = (TEMPLATES_DIR / "ai.html").read_text(encoding="utf-8")
    lakeset_js = (CONTROLLERS_DIR / "lakeset.js").read_text(encoding="utf-8")

    assert 'html[data-theme="light"] .echarts-tooltip-custom' in dashboard_view_html
    assert "getLakeEChartsThemeConfig" in dashboard_view_html
    assert "getLakeEChartsThemeConfig" in ai_html
    assert "getLakeEChartsThemeConfig" in lakeset_js


def test_pipelines_execution_logs_remain_permanently_dark():
    """Phase 2 (#460): Verify pipeline execution log terminal carries .keep-dark for permanent dark ergonomics."""
    pipelines_html = (TEMPLATES_DIR / "pipelines.html").read_text(encoding="utf-8")
    assert 'id="log-terminal"' in pipelines_html
    assert "log-scrollbar keep-dark" in pipelines_html


def test_page_by_page_light_theme_coverage():
    """Phase 3 (#461): Verify all core LakeUI templates contain light-theme rules."""
    expected_templates = [
        "base.html",
        "catalog.html",
        "pipelines.html",
        "sql.html",
        "notebooks.html",
        "storage.html",
        "dashboards.html",
        "dashboard_view.html",
        "ai.html",
        "lakeset.html",
        "bundles.html",
        "ingest.html",
        "home.html",
        "settings/admin.html",
        "settings/workspace.html",
        "settings/account.html",
    ]
    for rel_path in expected_templates:
        content = (TEMPLATES_DIR / rel_path).read_text(encoding="utf-8")
        assert (
            'html[data-theme="light"]' in content
        ), f"Missing html[data-theme='light'] coverage in {rel_path}"
