from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_TEMPLATE = REPO_ROOT / "ui" / "templates" / "ai.html"


def test_ai_template_includes_rich_markdown_renderer_and_styles():
    """Issue #529: Verify Genie UI loads marked.js, defines renderGenieMarkdown + fallback parser, and styles .genie-markdown."""
    html = AI_TEMPLATE.read_text(encoding="utf-8")

    assert "marked.min.js" in html
    assert "function renderGenieMarkdown(" in html
    assert "function fallbackMarkdownParse(" in html
    assert "function sanitizeRenderedHtml(" in html
    assert ".genie-markdown" in html
    assert 'html[data-theme="light"] .genie-markdown' in html


def test_ai_template_includes_collapsible_chat_history_sidebar_and_multi_thread_management():
    """Issue #529: Verify ChatGPT/Claude-style collapsible left chat history sidebar and thread lifecycle functions."""
    html = AI_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="genieHistorySidebar"' in html
    assert 'id="newChatSidebarBtn"' in html
    assert 'id="genieThreadList"' in html
    assert 'id="genieThreadSearch"' in html
    assert "function startNewGenieChat()" in html
    assert "function switchGenieThread(" in html
    assert "function deleteGenieThread(" in html
    assert "function clearAllGenieThreads()" in html
    assert "function renderThreadSidebar()" in html
    assert "lake_genie_threads_" in html


def test_ai_template_includes_auto_expanding_composer_and_up_down_arrow_prompt_history():
    """Issue #529: Verify aligned single-line text input composer and ArrowUp/ArrowDown prompt history recall."""
    html = AI_TEMPLATE.read_text(encoding="utf-8")

    assert 'type="text"' in html
    assert 'id="question"' in html
    assert "max-w-7xl" not in html
    assert html.index('id="genieChatThread"') < html.index('id="genieHistorySidebar"')
    assert "function recallPreviousPrompt(" in html
    assert "function getPromptHistoryList()" in html
    assert "function recordPromptInHistory(" in html
    assert 'e.key === "ArrowUp"' in html
    assert 'e.key === "ArrowDown"' in html
    assert "promptHistoryDraft" in html


def test_ai_template_includes_inline_edit_and_rerun_for_user_messages():
    """Issue #529: Verify clicking/editing past user prompts in chat truncates subsequent turns and re-runs Genie."""
    html = AI_TEMPLATE.read_text(encoding="utf-8")

    assert "function startEditGenieTurn(" in html
    assert "function cancelEditGenieTurn()" in html
    assert "function submitEditedGenieTurn(" in html
    assert "genieInlineEditInput-" in html
    assert "chatHistory.slice(0, idx)" in html
    assert "Save &amp; Submit" in html
    assert "function regenerateGenieTurn(" in html
    assert "function copyTurnText(" in html
    assert "function copyTurnSql(" in html


def test_ai_template_includes_interactive_chart_type_switcher_and_csv_copy():
    """Issue #529: Verify interactive chart switcher (Bar, Line, Pie, KPI, Table Only) and CSV copy on Genie chart cards."""
    html = AI_TEMPLATE.read_text(encoding="utf-8")

    assert "function switchTurnChartType(" in html
    assert "function copyTurnTableCsv(" in html
    assert "function renderGenieEChart(" in html
    assert "getLakeEChartsThemeConfig" in html
    assert "/lakeset?mode=explore&sql=" in html
    assert "Open in Lakeset" in html


def test_ai_route_renders_redesigned_genie_chatbot_layout():
    """Issue #529: Verify GET /ai renders the new sidebar, auto-expanding composer, and rich markdown styles."""
    import sys

    ui_dir = str(REPO_ROOT / "ui")
    if ui_dir not in sys.path:
        sys.path.insert(0, ui_dir)

    import main as ui_main
    from starlette.testclient import TestClient

    original_auth = ui_main.AUTH_ENABLED
    ui_main.AUTH_ENABLED = False
    try:
        client = TestClient(ui_main.app)
        resp = client.get("/ai")
        assert resp.status_code == 200
        body = resp.text
        assert 'id="genieHistorySidebar"' in body
        assert 'id="newChatSidebarBtn"' in body
        assert 'id="genieChatThread"' in body
        assert 'id="question"' in body
        assert "renderGenieMarkdown" in body
        assert "recallPreviousPrompt" in body
        assert "startEditGenieTurn" in body
        assert "switchTurnChartType" in body
    finally:
        ui_main.AUTH_ENABLED = original_auth

