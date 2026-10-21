import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Ensure ui and backend are in path
ROOT_DIR = Path(__file__).parents[1]
if str(ROOT_DIR / "ui") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "ui"))
if str(ROOT_DIR / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "backend"))

import notebook_proxy  # noqa: E402
import notebook_service  # noqa: E402


def test_ui_ignored_dirs_and_extensions_sets():
    assert "data" in notebook_proxy.IGNORED_WORKSPACE_DIRS
    assert "raw" in notebook_proxy.IGNORED_WORKSPACE_DIRS
    assert "scratch" in notebook_proxy.IGNORED_WORKSPACE_DIRS
    assert "tmp" in notebook_proxy.IGNORED_WORKSPACE_DIRS

    assert ".parquet" in notebook_proxy.IGNORED_WORKSPACE_FILE_EXTENSIONS
    assert ".csv" in notebook_proxy.IGNORED_WORKSPACE_FILE_EXTENSIONS
    assert ".duckdb" in notebook_proxy.IGNORED_WORKSPACE_FILE_EXTENSIONS
    assert ".db" in notebook_proxy.IGNORED_WORKSPACE_FILE_EXTENSIONS


def test_backend_ignored_dirs_and_extensions_sets():
    assert "data" in notebook_service.IGNORED_WORKSPACE_DIRS
    assert "raw" in notebook_service.IGNORED_WORKSPACE_DIRS
    assert ".parquet" in notebook_service.IGNORED_WORKSPACE_FILE_EXTENSIONS
    assert ".duckdb" in notebook_service.IGNORED_WORKSPACE_FILE_EXTENSIONS


def test_scan_workspace_filesystem_filters_data_and_parquet(tmp_path):
    ws_root = tmp_path / "workspaces" / "dev"
    ws_root.mkdir(parents=True)

    # Legitimate notebook and project files
    (ws_root / "analysis.ipynb").write_text("{}", encoding="utf-8")
    (ws_root / "utils.py").write_text("# utils", encoding="utf-8")
    (ws_root / "README.md").write_text("# Docs", encoding="utf-8")

    subfolder = ws_root / "pipelines"
    subfolder.mkdir()
    (subfolder / "pipeline.ipynb").write_text("{}", encoding="utf-8")

    # Data files and data directory that must be ignored
    data_dir = subfolder / "data"
    data_dir.mkdir()
    (data_dir / "records.parquet").write_bytes(b"PAR1")
    (data_dir / "nested.csv").write_text("a,b\n1,2", encoding="utf-8")
    (subfolder / "extracted.parquet").write_bytes(b"PAR1")
    (subfolder / "cache.duckdb").write_bytes(b"DUCK")

    with patch.object(notebook_proxy, "WORKSPACES_DIR", str(tmp_path / "workspaces")):
        items = notebook_proxy.scan_workspace_filesystem("dev", force_refresh=True)

    assert items is not None
    names = {item["name"] for item in items}
    paths = {item["path"] for item in items}

    # Verify permitted files exist
    assert "analysis.ipynb" in names
    assert "utils.py" in names
    assert "README.md" in names
    assert "pipelines" in names
    assert "pipeline.ipynb" in names

    # Verify data files and data directories are filtered out
    assert "data" not in names
    assert "records.parquet" not in names
    assert "nested.csv" not in names
    assert "extracted.parquet" not in names
    assert "cache.duckdb" not in names

    for p in paths:
        assert not p.endswith(".parquet")
        assert not p.endswith(".duckdb")
        assert not p.endswith(".csv")
        assert "data/" not in p and not p.startswith("data")


@pytest.mark.anyio
async def test_ui_list_workspace_notebooks_filters_data():
    mock_jupyter_contents = {
        "content": [
            {"name": "clean.ipynb", "type": "notebook", "path": "workspaces/dev/clean.ipynb"},
            {"name": "script.py", "type": "file", "path": "workspaces/dev/script.py"},
            {"name": "data", "type": "directory", "path": "workspaces/dev/data"},
            {"name": "trials.parquet", "type": "file", "path": "workspaces/dev/trials.parquet"},
            {"name": "db.duckdb", "type": "file", "path": "workspaces/dev/db.duckdb"},
        ]
    }

    with (
        patch.object(notebook_proxy, "ensure_workspace_dir", new_callable=AsyncMock),
        patch.object(
            notebook_proxy,
            "jupyter_request",
            new_callable=AsyncMock,
            return_value=(200, mock_jupyter_contents),
        ),
    ):
        result = await notebook_proxy.list_workspace_notebooks("dev")

    items = result.get("items", [])
    names = [i["name"] for i in items]

    assert "clean.ipynb" in names
    assert "script.py" in names
    assert "data" not in names
    assert "trials.parquet" not in names
    assert "db.duckdb" not in names


@pytest.mark.anyio
async def test_backend_list_workspace_notebooks_filters_data():
    mock_jupyter_contents = {
        "content": [
            {"name": "clean.ipynb", "type": "notebook", "path": "workspaces/dev/clean.ipynb"},
            {"name": "script.py", "type": "file", "path": "workspaces/dev/script.py"},
            {"name": "data", "type": "directory", "path": "workspaces/dev/data"},
            {"name": "trials.parquet", "type": "file", "path": "workspaces/dev/trials.parquet"},
            {"name": "db.duckdb", "type": "file", "path": "workspaces/dev/db.duckdb"},
        ]
    }

    with (
        patch.object(notebook_service, "ensure_workspace_dir", new_callable=AsyncMock),
        patch.object(
            notebook_service,
            "jupyter_request",
            new_callable=AsyncMock,
            return_value=(200, mock_jupyter_contents),
        ),
    ):
        result = await notebook_service.list_workspace_notebooks("dev")

    items = result.get("items", [])
    names = [i["name"] for i in items]

    assert "clean.ipynb" in names
    assert "script.py" in names
    assert "data" not in names
    assert "trials.parquet" not in names
    assert "db.duckdb" not in names


def test_workspace_isolation_startup_script_guard(tmp_path):
    startup_script = ROOT_DIR / "deploy" / "jupyter" / "00-workspace-isolation.py"
    assert startup_script.is_file(), "Startup script 00-workspace-isolation.py must exist"

    # Run a Python subprocess that loads the startup script and verifies write-guards
    driver_code = f"""
import os
import pathlib
import sys

# Mock workspace root pointing to a simulated Jovyan workspaces directory
fake_ws = pathlib.Path("{tmp_path}/home/jovyan/workspaces/dev/pipelines")
fake_ws.mkdir(parents=True, exist_ok=True)
os.chdir(fake_ws)

# Load startup script
startup_path = "{startup_script}"
with open(startup_path, "r", encoding="utf-8") as f:
    code = f.read()

# Replace /home/jovyan/workspaces with our fake_ws root in the script for isolation
code = code.replace(
    'pathlib.Path("/home/jovyan/workspaces")',
    'pathlib.Path("{tmp_path}/home/jovyan/workspaces")'
)

globs = {{"__file__": startup_path, "__name__": "__main__"}}
exec(code, globs)

# Verify CWD changed to /tmp/scratch/dev
expected_scratch = pathlib.Path("/tmp/scratch/dev").resolve()
assert pathlib.Path(os.getcwd()).resolve() == expected_scratch, f"Expected CWD {{expected_scratch}}, got {{os.getcwd()}}"
assert os.environ.get("WORKSPACE_SLUG") == "dev"
assert os.environ.get("LAKE_SCRATCH_DIR") == "/tmp/scratch/dev"

# Verify writing inside workspace raises PermissionError
forbidden_file = fake_ws / "trials.parquet"
try:
    with open(forbidden_file, "w") as f:
        f.write("data")
    print("FAILED: Write inside workspace should have raised PermissionError")
    sys.exit(1)
except PermissionError as e:
    assert "Lake of Tears Workspace Isolation" in str(e)

# Verify creating directory inside workspace raises PermissionError
forbidden_dir = fake_ws / "data"
try:
    os.mkdir(forbidden_dir)
    print("FAILED: os.mkdir inside workspace should have raised PermissionError")
    sys.exit(2)
except PermissionError as e:
    assert "Lake of Tears Workspace Isolation" in str(e)

try:
    (fake_ws / "data_pathlib").mkdir()
    print("FAILED: Path.mkdir inside workspace should have raised PermissionError")
    sys.exit(3)
except PermissionError as e:
    assert "Lake of Tears Workspace Isolation" in str(e)

# Verify writing in scratch directory works
scratch_file = pathlib.Path("/tmp/scratch/dev/sample.parquet")
with open(scratch_file, "w") as f:
    f.write("allowed")
assert scratch_file.read_text() == "allowed"

print("SUCCESS")
"""

    res = subprocess.run(
        [sys.executable, "-c", driver_code],
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, f"Script failed with stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "SUCCESS" in res.stdout
