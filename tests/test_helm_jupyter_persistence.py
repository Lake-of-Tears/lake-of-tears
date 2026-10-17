import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).parents[1] / "deploy/helm/lake-of-tears"
VALUES_LOCAL = Path(__file__).parents[1] / "deploy/helm/values-local.example.yaml"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _render(*extra: str) -> list[dict]:
    out = subprocess.run(
        [
            "helm",
            "template",
            "t",
            str(CHART_DIR),
            "--namespace",
            "lake-of-tears",
            "-f",
            str(VALUES_LOCAL),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _jupyter_deployment(docs):
    return next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-jupyter")
    )


def test_jupyter_gets_a_claim_of_the_configured_size():
    docs = _render("--set", "jupyter.persistence.size=7Gi")
    claim = next(
        d
        for d in docs
        if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("jupyter-home")
    )
    assert claim["spec"]["resources"]["requests"]["storage"] == "7Gi"


def test_claim_honours_the_global_storage_class():
    docs = _render("--set", "global.storageClass=fast")
    claim = next(
        d
        for d in docs
        if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"].endswith("jupyter-home")
    )
    assert claim["spec"]["storageClassName"] == "fast"


def test_jupyter_mounts_the_folders_the_lake_ui_writes_to():
    pod = _jupyter_deployment(_render())["spec"]["template"]["spec"]
    mounts = {m["mountPath"]: m["subPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts == {
        "/home/jovyan/workspaces": "workspaces",
        "/home/jovyan/work": "work",
    }
    volume = next(v for v in pod["volumes"] if v["name"] == "jupyter-home")
    assert volume["persistentVolumeClaim"]["claimName"].endswith("-jupyter-home")
    assert pod["securityContext"]["fsGroup"] == 100
