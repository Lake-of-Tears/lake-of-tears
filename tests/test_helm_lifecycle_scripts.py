"""Unit and integration tests for Lake of Tears Helm lifecycle management scripts.

Tests cover:
- TOON configuration resolution & discovery
- Secret scanning & cryptographic generation
- TrueNAS-style platform update checking (stable and edge trains)
- URL extraction from TOON specifications
- CLI runner (`./lot`) and lifecycle script execution
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pipeline.shared.lifecycle import (
    check_platform_updates,
    find_toon_config,
    generate_secure_secrets,
    scan_placeholder_secrets,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_find_toon_config_precedence(tmp_path: Path):
    """Test precedence rules for TOON configuration discovery."""
    # 1. Explicit path overrides all
    custom_toon = tmp_path / "custom.toon"
    custom_toon.write_text("global:\n  storageClass: custom\n")

    found = find_toon_config(explicit_path=custom_toon, repo_root=tmp_path)
    assert found == custom_toon

    # 2. Environment variable overrides repo root defaults
    env_toon = tmp_path / "env.toon"
    env_toon.write_text("global:\n  storageClass: env\n")
    os.environ["LOT_CONFIG"] = str(env_toon)
    try:
        found = find_toon_config(repo_root=tmp_path)
        assert found == env_toon
    finally:
        del os.environ["LOT_CONFIG"]

    # 3. Repo root lake-of-tears.toon
    root_toon = tmp_path / "lake-of-tears.toon"
    root_toon.write_text("global:\n  storageClass: root\n")
    found = find_toon_config(repo_root=tmp_path)
    assert found == root_toon

    # 4. Fallback to lake-of-tears.local.toon if root .toon is deleted
    root_toon.unlink()
    local_toon = tmp_path / "lake-of-tears.local.toon"
    local_toon.write_text("global:\n  storageClass: local\n")
    found = find_toon_config(repo_root=tmp_path)
    assert found == local_toon


def test_scan_placeholder_secrets():
    """Verify scanning and deduplication of changeme-* credentials."""
    sample_content = """
minio:
  rootPassword: "changeme-minio-root-password"
jupyter:
  token: "changeme-jupyter-secret-token"
superset:
  secretKey: "changeme-flask-secret-key-superset"
  adminPassword: "changeme-superset-admin-password"
duplicate:
  pwd: "changeme-minio-root-password"
clean:
  val: "already-configured-secret-12345"
"""
    placeholders = scan_placeholder_secrets(sample_content)
    assert len(placeholders) == 4
    assert "changeme-minio-root-password" in placeholders
    assert "changeme-jupyter-secret-token" in placeholders
    assert "changeme-flask-secret-key-superset" in placeholders
    assert "changeme-superset-admin-password" in placeholders


def test_generate_secure_secrets():
    """Verify cryptographically secure secret auto-generation and replacement."""
    sample_content = """
minio:
  rootPassword: "changeme-minio-root-password"
auth:
  jwt: "changeme-backend-auth-jwt-secret-key-32-bytes"
"""
    updated, replacements = generate_secure_secrets(sample_content)

    assert "changeme-minio-root-password" not in updated
    assert "changeme-backend-auth-jwt-secret-key-32-bytes" not in updated

    # JWT key must be 64 hex characters (32 bytes)
    jwt_secret = replacements["changeme-backend-auth-jwt-secret-key-32-bytes"]
    assert len(jwt_secret) == 64
    assert all(c in "0123456789abcdef" for c in jwt_secret)

    # User password replacement must be random and non-empty
    minio_secret = replacements["changeme-minio-root-password"]
    assert len(minio_secret) >= 24
    assert minio_secret != "changeme-minio-root-password"


def test_check_platform_updates_structure():
    """Verify TrueNAS-style platform update dictionary structure."""
    result = check_platform_updates(train="stable", repo_root=REPO_ROOT, fetch_remote=False)

    assert result["train"] == "stable"
    assert result["status"] in ("AVAILABLE", "UNAVAILABLE", "ERROR")
    assert "current_version" in result
    assert "latest_version" in result
    assert isinstance(result["changelog"], list)
    assert isinstance(result["can_update"], bool)

    # Check edge train
    result_edge = check_platform_updates(train="edge", repo_root=REPO_ROOT, fetch_remote=False)
    assert result_edge["train"] == "edge"
    assert result_edge["status"] in ("AVAILABLE", "UNAVAILABLE", "ERROR")


def test_cli_runner_help():
    """Verify ./lot CLI root runner help command."""
    proc = subprocess.run(
        [str(REPO_ROOT / "lot"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Lake of Tears (LOT) — Unified Platform Lifecycle Manager" in proc.stdout
    assert "install" in proc.stdout
    assert "update" in proc.stdout
    assert "destroy" in proc.stdout
    assert "status" in proc.stdout
    assert "config" in proc.stdout


def test_lifecycle_helper_extract_urls():
    """Verify lifecycle helper extract-urls parses service endpoints correctly."""
    example_toon = REPO_ROOT / "lake-of-tears.example.toon"
    proc = subprocess.run(
        [
            sys_python(),
            str(REPO_ROOT / "scripts" / "lifecycle_helper.py"),
            "extract-urls",
            "-i",
            str(example_toon),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["lake_ui"] == "https://lake.example.com"
    assert data["pipelines"] == "https://lake.example.com/airflow/"
    assert data["minio_console"] == "https://minio.example.com"
    assert data["superset"] == "https://superset.example.com"
    assert data["ingress_enabled"] is True


def test_update_script_check_only_json():
    """Verify deploy/scripts/update.sh returns valid JSON with --check-only --json."""
    proc = subprocess.run(
        [
            str(REPO_ROOT / "deploy" / "scripts" / "update.sh"),
            "--check-only",
            "--json",
            "--no-fetch",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["train"] == "stable"
    assert "status" in data
    assert "current_version" in data
    assert "latest_version" in data


def test_destroy_script_purge_safety():
    """Verify deploy/scripts/destroy.sh aborts if --purge-data is passed without --force in non-interactive mode."""
    proc = subprocess.run(
        [
            str(REPO_ROOT / "deploy" / "scripts" / "destroy.sh"),
            "--purge-data",
            "--namespace",
            "test-safety-ns",
        ],
        capture_output=True,
        text=True,
    )
    # Must exit with non-zero code to protect data
    assert proc.returncode != 0
    assert "Purging data in a non-interactive shell requires the --force flag" in proc.stderr


def sys_python() -> str:
    """Helper to return active virtualenv Python."""
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.is_file() else "python3"
