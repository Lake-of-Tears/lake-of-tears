from pathlib import Path

import yaml

COMPOSE = Path(__file__).parents[1] / "docker-compose.yml"


def _jupyter_volumes() -> list[str]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["jupyter"]["volumes"]


def test_lake_ui_notebooks_folder_is_a_host_mount():
    """The Lake UI creates notebooks under workspaces/ in Jupyter's root (/home/jovyan)."""
    targets = {v.split(":")[-1] for v in _jupyter_volumes()}
    assert "/home/jovyan/workspaces" in targets


def test_existing_mounts_are_kept():
    targets = {v.split(":")[-1] for v in _jupyter_volumes()}
    assert {"/home/jovyan/work", "/home/jovyan/datalake"} <= targets


def test_jupyter_mounts_workspace_isolation_startup_script():
    volumes = _jupyter_volumes()
    found = any(
        "/home/jovyan/.ipython/profile_default/startup/00-workspace-isolation.py" in v
        for v in volumes
    )
    assert found, "Expected 00-workspace-isolation.py to be mounted in jupyter volumes"
