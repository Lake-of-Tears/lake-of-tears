"""Automated JavaScript Syntax Validation Suite.

Validates that:
1. All static JavaScript files in `ui/static/js/` parse with zero syntax errors.
2. All inline `<script>` blocks across rendered UI routes parse with zero syntax errors,
   catching unmatched braces, unexpected tokens, and invalid template-interpolated JavaScript.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from starlette.testclient import TestClient

repo_root = Path(__file__).resolve().parents[1]
ui_dir = repo_root / "ui"
if str(ui_dir) not in sys.path:
    sys.path.insert(0, str(ui_dir))

import main  # noqa: E402

client = TestClient(main.app)
NODE_BIN = shutil.which("node")

UI_ROUTES_TO_VERIFY = [
    "/",
    "/pipelines",
    "/catalog",
    "/bundles",
    "/dashboards",
    "/sql",
    "/notebooks",
    "/ingest",
    "/anomalies",
    "/ai",
    "/storage",
    "/settings/workspace",
    "/settings/admin",
    "/settings/account",
    "/login",
]


def _require_node() -> str:
    if not NODE_BIN:
        pytest.skip("Node.js runtime not found on PATH; skipping JavaScript syntax verification.")
    return NODE_BIN


def test_static_controllers_syntax():
    """Verify every static controller file in ui/static/js/ has valid JavaScript syntax."""
    node = _require_node()
    js_dir = ui_dir / "static" / "js"
    assert js_dir.exists(), "ui/static/js directory must exist"

    js_files = sorted(
        [
            p
            for p in js_dir.rglob("*.js")
            if "vendor" not in p.parts  # skip vendor dependencies
        ]
    )
    assert len(js_files) > 0, "Expected to find static JavaScript controllers"

    failures = []
    for js_file in js_files:
        res = subprocess.run([node, "-c", str(js_file)], capture_output=True, text=True)
        if res.returncode != 0:
            failures.append(f"{js_file.relative_to(repo_root)}:\n{res.stderr.strip()}")

    assert not failures, (
        "JavaScript syntax errors detected in static controller files:\n" + "\n\n".join(failures)
    )


@pytest.mark.parametrize("route", UI_ROUTES_TO_VERIFY)
def test_rendered_page_inline_scripts_syntax(route: str):
    """Render each UI page and verify that all inline <script> blocks parse with zero syntax errors."""
    node = _require_node()

    token = jwt.encode(
        {"sub": "admin@lake.local", "email": "admin@lake.local", "role": "admin"},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default", "my_role": "owner"}]

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            route,
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=True,
        )

    assert (
        response.status_code == 200
    ), f"Route {route} failed to render (HTTP {response.status_code})"

    # Extract all inline script blocks (excluding external script imports with src=...)
    scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", response.text, re.DOTALL)

    failures = []
    for idx, script_content in enumerate(scripts):
        cleaned = script_content.strip()
        if not cleaned:
            continue

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tf.write(cleaned)
            tf_path = tf.name

        try:
            res = subprocess.run([node, "-c", tf_path], capture_output=True, text=True)
            if res.returncode != 0:
                first_lines = "\n".join(cleaned.splitlines()[:15])
                failures.append(
                    f"Route {route} - Script #{idx + 1}:\n"
                    f"Error: {res.stderr.strip()}\n"
                    f"Script snippet:\n{first_lines}..."
                )
        finally:
            Path(tf_path).unlink(missing_ok=True)

    assert not failures, (
        f"JavaScript syntax errors found in rendered template scripts for route '{route}':\n"
        + "\n\n".join(failures)
    )
