from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    sync_playwright = None

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_TEMPLATE = REPO_ROOT / "ui" / "templates" / "ai.html"

ui_dir = str(REPO_ROOT / "ui")
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import main as ui_main  # noqa: E402


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
    """Issue #529: Verify collapsible right-hand chat history sidebar and thread lifecycle functions."""
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


def test_ai_template_includes_aligned_text_input_composer_and_up_down_arrow_prompt_history():
    """Issue #529: Verify aligned single-line text input composer, full width layout, and ArrowUp/ArrowDown prompt history recall."""
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
    """Issue #529: Verify GET /ai renders the right sidebar, aligned input, and rich markdown styles."""
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


@pytest.fixture(scope="module")
def genie_ui_server():
    """Launches an ephemeral Uvicorn server serving the Lake UI for Playwright E2E tests."""
    if not HAS_PLAYWRIGHT:
        pytest.skip("playwright is not installed")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_url = f"http://127.0.0.1:{port}"
    orig_auth = ui_main.AUTH_ENABLED
    ui_main.AUTH_ENABLED = False

    mock_ws_list = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "Default Workspace",
            "slug": "default",
            "my_role": "admin",
            "is_shared": False,
        }
    ]
    patch_ws = patch.object(ui_main, "_fetch_workspaces", return_value=mock_ws_list)
    patch_ws.start()
    patch_cats = patch.object(ui_main, "_resolve_ui_catalogs", return_value=[])
    patch_cats.start()

    import uvicorn

    config = uvicorn.Config(
        app=ui_main.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()

    import httpx

    ready = False
    for _ in range(50):
        try:
            r = httpx.get(f"{server_url}/ai", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.1)

    if not ready:
        pytest.fail("Genie UI server failed to start within 5 seconds")

    yield server_url

    uv_server.should_exit = True
    thread.join(timeout=2.0)
    patch_cats.stop()
    patch_ws.stop()
    ui_main.AUTH_ENABLED = orig_auth


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright is not installed")
def test_e2e_genie_layout_markdown_prompt_history_edit_and_sidebar(genie_ui_server):
    """Playwright E2E verification of Issue #529:
    1. Right-hand Chat History sidebar & flush alignment between #question input and #askBtn.
    2. Rich Markdown rendering (headings, bold, inline code, bullet lists — no loose markdown).
    3. Interactive chart switcher (Bar, Line, Pie, KPI, Table Only).
    4. Up/Down arrow prompt history recall preserving current draft.
    5. Clicking a past user message to inline-edit & re-run (truncating subsequent turns).
    6. Multi-chat thread creation (+ New Chat) and switching in the right sidebar.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            base_url=genie_ui_server,
        )
        page = context.new_page()
        uncaught_errors: list[str] = []
        page.on("pageerror", lambda exc: uncaught_errors.append(str(exc)))

        call_count = {"n": 0}

        def handle_stream_route(route):
            call_count["n"] += 1
            body_json = json.loads(route.request.post_data or "{}")
            q_text = body_json.get("question", "")
            payload = {
                "ok": True,
                "mode": "genie",
                "answer": (
                    f"### Clinical Trials Summary ({call_count['n']})\n\n"
                    f"For query **{q_text}**, we found **300** active records in `gold.clinical_trials`.\n\n"
                    "- **Phase 1**: 120 trials\n"
                    "- **Phase 2**: 180 trials"
                ),
                "sql": "SELECT phase, COUNT(*) AS trial_count FROM gold.clinical_trials GROUP BY 1",
                "self_corrected": False,
                "columns": ["phase", "trial_count"],
                "rows": [["Phase 1", 120], ["Phase 2", 180]],
                "row_count": 2,
                "execution_ms": 14.2,
                "chart_config": {
                    "type": "bar",
                    "title": "Trials by Phase",
                    "x_column": "phase",
                    "y_columns": ["trial_count"],
                },
                "snippets": [],
            }
            sse_body = f"event: result\ndata: {json.dumps(payload)}\n\n"
            route.fulfill(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=sse_body,
            )

        page.route("**/api/v1/ai/chat/stream", handle_stream_route)

        page.goto("/ai", wait_until="networkidle")

        # 1. Verify Right Sidebar placement & Input/Button alignment
        chat_box = page.locator("#genieChatThread").bounding_box()
        sidebar_box = page.locator("#genieHistorySidebar").bounding_box()
        assert chat_box is not None and sidebar_box is not None
        assert (
            chat_box["x"] < sidebar_box["x"]
        ), "Chat History sidebar must be on the right side of the chat thread"

        input_box = page.locator("#question").bounding_box()
        ask_btn_box = page.locator("#askBtn").bounding_box()
        assert input_box is not None and ask_btn_box is not None
        assert (
            abs(input_box["y"] - ask_btn_box["y"]) <= 2
        ), f"Input y ({input_box['y']}) and Ask button y ({ask_btn_box['y']}) should be aligned"
        assert (
            abs(input_box["height"] - ask_btn_box["height"]) <= 2
        ), f"Input height ({input_box['height']}) and Ask button height ({ask_btn_box['height']}) should match"

        # 2. Submit a prompt and verify Rich Markdown rendering (no loose ### or **)
        question_input = page.locator("#question")
        question_input.fill("How many clinical trials records do I have?")
        page.locator("#askBtn").click()

        md_container = page.locator("#genieChatThread .genie-markdown").first
        md_container.wait_for(state="visible", timeout=5000)

        assert md_container.locator("h3", has_text="Clinical Trials Summary (1)").is_visible()
        assert md_container.locator("strong", has_text="300").is_visible()
        assert md_container.locator("code", has_text="gold.clinical_trials").is_visible()
        assert md_container.locator("ul li").count() == 2
        assert "###" not in md_container.inner_text()
        assert "**" not in md_container.inner_text()

        # 3. Verify Interactive Chart Type Switcher (KPI, Table Only, Bar)
        page.locator(".genie-chart-card button:has-text('KPI')").click()
        assert page.locator(".genie-chart-card", has_text="120").is_visible()
        page.locator(".genie-chart-card button:has-text('Table Only')").click()
        assert page.locator("[id^='genie-chart-']").count() == 0
        page.locator(".genie-chart-card button:has-text('Bar')").click()
        assert page.locator("[id^='genie-chart-']").count() == 1

        # 4. Verify Up/Down Arrow Prompt History Recall in #question
        question_input.fill("unsent draft text")
        question_input.press("ArrowUp")
        assert question_input.input_value() == "How many clinical trials records do I have?"
        question_input.press("ArrowDown")
        assert question_input.input_value() == "unsent draft text"
        question_input.fill("")

        # 5. Click past user message bubble -> Inline Edit & Rerun
        user_bubble = page.locator(".genie-user-bubble").first
        user_bubble.click()

        inline_input = page.locator("#genieInlineEditInput-0")
        inline_input.wait_for(state="visible", timeout=5000)
        assert inline_input.input_value() == "How many clinical trials records do I have?"

        inline_input.fill("Break down clinical trials by phase")
        page.locator("button:has-text('Save & Submit')").click()

        # Verify conversation truncated and re-ran with the updated prompt
        page.locator(
            "#genieChatThread .genie-markdown h3", has_text="Clinical Trials Summary (2)"
        ).wait_for(state="visible", timeout=5000)
        assert page.locator(".genie-user-bubble").count() == 1
        assert (
            "Break down clinical trials by phase"
            in page.locator(".genie-user-bubble").first.inner_text()
        )

        # 6. Verify Right Sidebar Thread List & '+ New Chat' switching
        assert page.locator("#genieThreadList", has_text="Break down clinical trials").count() >= 1
        page.locator("#newChatSidebarBtn").click()
        assert page.locator("#genieEmptyState").is_visible()

        # Click saved thread in right sidebar to restore conversation
        page.locator(
            "#genieThreadList .genie-thread-item", has_text="Break down clinical trials"
        ).first.click()
        assert page.locator(
            "#genieChatThread .genie-markdown h3", has_text="Clinical Trials Summary (2)"
        ).is_visible()

        context.close()
        browser.close()
        assert not uncaught_errors, f"Uncaught JS errors: {uncaught_errors}"
