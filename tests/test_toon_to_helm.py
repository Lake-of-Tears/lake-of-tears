from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from pipeline.shared.toon import parse_toon
from pipeline.shared.toon2helm import (
    _normalize_resources,
    toon_to_helm_dict,
    toon_to_helm_yaml,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_normalize_resources():
    flat_res = {
        "cpu_request": "250m",
        "mem_request": "1Gi",
        "cpu_limit": "2",
        "mem_limit": "4Gi",
    }
    normalized = _normalize_resources(flat_res)
    assert normalized["requests"]["cpu"] == "250m"
    assert normalized["requests"]["memory"] == "1Gi"
    assert normalized["limits"]["cpu"] == "2"
    assert normalized["limits"]["memory"] == "4Gi"

    # Already normalized pass-through
    already_norm = {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "1", "memory": "512Mi"},
    }
    assert _normalize_resources(already_norm) == already_norm


def test_toon_to_helm_dict_translation():
    sample_toon = """
ingress:
  enabled: true
  minioConsoleHost: "minio.test.internal"
  supersetHost: "superset.test.internal"
  tls[1]{secretName,hosts}:
    test-tls,"a.test.internal,b.test.internal"

lakeui:
  service:
    port: 3000

backend:
  enabled: true
  resources:
    cpu_request: "100m"
    mem_request: "256Mi"
    cpu_limit: "1"
    mem_limit: "512Mi"
  oauth[2]{provider,clientId,clientSecret,tenantId}:
    google,g-id,g-secret,
    microsoft,ms-id,ms-secret,common
"""
    parsed = parse_toon(sample_toon)
    helm_dict = toon_to_helm_dict(parsed)

    # Ingress TLS hosts converted to list
    assert helm_dict["ingress"]["tls"][0]["hosts"] == [
        "a.test.internal",
        "b.test.internal",
    ]

    # Lake UI URLs derived automatically
    assert helm_dict["lakeui"]["minioConsoleUrl"] == "https://minio.test.internal"
    assert helm_dict["lakeui"]["supersetUrl"] == "https://superset.test.internal"

    # Backend resources normalized
    assert helm_dict["backend"]["resources"]["requests"]["cpu"] == "100m"
    assert helm_dict["backend"]["resources"]["limits"]["memory"] == "512Mi"

    # OAuth providers unpacked
    assert helm_dict["backend"]["google"] == {
        "clientId": "g-id",
        "clientSecret": "g-secret",
    }
    assert helm_dict["backend"]["microsoft"] == {
        "clientId": "ms-id",
        "clientSecret": "ms-secret",
        "tenantId": "common",
    }
    assert "oauth" not in helm_dict["backend"]


def test_canonical_lake_of_tears_example_compilation():
    root_example = REPO_ROOT / "lake-of-tears.example.toon"
    assert root_example.exists(), "lake-of-tears.example.toon must exist in repository root"

    with open(root_example, encoding="utf-8") as f:
        content = f.read()

    yaml_str = toon_to_helm_yaml(content)
    loaded = yaml.safe_load(yaml_str)

    expected_sections = [
        "global",
        "ingress",
        "minio",
        "lakeui",
        "jupyter",
        "superset",
        "airflow",
        "backend",
        "postgres",
        "connectors",
        "stripe",
        "shopify",
        "hubspot",
    ]
    for section in expected_sections:
        assert section in loaded, f"Missing expected section '{section}' in compiled Helm values"

    assert loaded["global"]["storageClass"] == "local-path"
    assert loaded["ingress"]["enabled"] is True
    assert loaded["minio"]["resources"]["requests"]["cpu"] == "500m"
    assert loaded["lakeui"]["minioConsoleUrl"] == "https://minio.example.com"
    assert loaded["backend"]["google"]["clientId"] == "YOUR_GOOGLE_CLIENT_ID"
    assert loaded["backend"]["github"]["clientSecret"] == "YOUR_GITHUB_CLIENT_SECRET"
    assert len(loaded["connectors"]) == 11


def test_cli_invocation():
    script_path = REPO_ROOT / "scripts" / "toon2helm.py"
    example_path = REPO_ROOT / "lake-of-tears.example.toon"

    res = subprocess.run(
        [sys.executable, str(script_path), "--input", str(example_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = yaml.safe_load(res.stdout)
    assert loaded["global"]["storageClass"] == "local-path"
    assert "minio" in loaded
