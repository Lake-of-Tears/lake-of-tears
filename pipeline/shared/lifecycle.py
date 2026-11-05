"""Platform lifecycle management, TOON configuration validation, and update checking.

Provides functions for locating configurations, scanning and auto-generating secure secrets,
and checking for available updates (supporting TrueNAS-style 'stable' and 'edge' trains).
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

# Regular expression matching placeholder passwords/tokens
PLACEHOLDER_REGEX = re.compile(r"changeme-[a-zA-Z0-9_-]+")


def get_repo_root() -> Path:
    """Return the absolute path to the repository root directory."""
    return Path(__file__).resolve().parent.parent.parent


def find_toon_config(
    explicit_path: str | Path | None = None, repo_root: Path | None = None
) -> Path | None:
    """Resolve the TOON configuration file path following Lake of Tears precedence order.

    Precedence:
      1. Explicit argument (`explicit_path`)
      2. Environment variable `$LOT_CONFIG`
      3. Repo root `lake-of-tears.toon`
      4. System path `/etc/lake-of-tears/lake-of-tears.toon`
      5. Repo root `lake-of-tears.local.toon`
    """
    root = repo_root or get_repo_root()

    # 1. Explicit path
    if explicit_path:
        p = Path(explicit_path).expanduser().resolve()
        if p.is_file():
            return p

    # 2. Environment variable
    env_path = os.getenv("LOT_CONFIG")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            return p

    # 3. Repo root lake-of-tears.toon
    root_toon = root / "lake-of-tears.toon"
    if root_toon.is_file():
        return root_toon

    # 4. System-wide configuration
    sys_toon = Path("/etc/lake-of-tears/lake-of-tears.toon")
    if sys_toon.is_file():
        return sys_toon

    # 5. Local development configuration
    local_toon = root / "lake-of-tears.local.toon"
    if local_toon.is_file():
        return local_toon

    return None


def scan_placeholder_secrets(toon_content: str) -> list[str]:
    """Return a deduplicated list of placeholder tokens (e.g. changeme-*) found in TOON content."""
    matches = PLACEHOLDER_REGEX.findall(toon_content)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def generate_secure_secrets(toon_content: str) -> tuple[str, dict[str, str]]:
    """Replace all `changeme-*` placeholder tokens with cryptographically secure random values.

    Returns:
      (updated_content, mapping_of_placeholder_to_generated_secret)
    """
    placeholders = scan_placeholder_secrets(toon_content)
    replacements: dict[str, str] = {}

    for ph in placeholders:
        if "jwt" in ph.lower() or "fernet" in ph.lower():
            # 32 bytes (64 hex characters) for cryptographic keys
            replacements[ph] = secrets.token_hex(32)
        else:
            # 20 bytes (27-28 urlsafe chars) for user/service passwords
            replacements[ph] = secrets.token_urlsafe(20)

    updated_content = toon_content
    for ph, secret_val in replacements.items():
        updated_content = updated_content.replace(ph, secret_val)

    return updated_content, replacements


def _run_git_cmd(args: list[str], cwd: Path) -> str:
    """Helper to run a git command and return stripped stdout."""
    res = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def check_platform_updates(
    train: str = "stable",
    repo_root: Path | None = None,
    fetch_remote: bool = True,
) -> dict[str, Any]:
    """Check for available platform updates in the given train (TrueNAS-style).

    Trains:
      - 'stable': Checks for newer Git semantic/build tags (e.g., v1.0.0-build.*).
      - 'edge': Checks for newer commits on remote tracking branch (origin/main).

    Returns a structured dictionary:
      {
        "status": "AVAILABLE" | "UNAVAILABLE" | "ERROR",
        "train": "stable" | "edge",
        "current_version": str,
        "latest_version": str,
        "changelog": list[str],
        "can_update": bool,
        "error": str | None
      }
    """
    root = repo_root or get_repo_root()
    train = train.lower().strip()
    if train not in ("stable", "edge"):
        train = "stable"

    result: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "train": train,
        "current_version": "unknown",
        "latest_version": "unknown",
        "changelog": [],
        "can_update": False,
        "error": None,
    }

    try:
        # Determine current commit / tag
        try:
            current_tag = _run_git_cmd(["describe", "--tags", "--exact-match"], root)
        except subprocess.CalledProcessError:
            try:
                current_tag = _run_git_cmd(["describe", "--tags", "--always"], root)
            except subprocess.CalledProcessError:
                current_tag = _run_git_cmd(["rev-parse", "--short", "HEAD"], root)

        result["current_version"] = current_tag

        if fetch_remote:
            try:
                # Fetch tags and current branch updates from origin with short timeout
                subprocess.run(
                    ["git", "fetch", "--tags", "origin", "main"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except Exception:
                # If network fetch fails, continue with local cache
                pass

        if train == "stable":
            # Sort tags using git's version:refname order
            sorted_tags_output = _run_git_cmd(["tag", "-l", "v*", "--sort=-version:refname"], root)
            sorted_tags = [t.strip() for t in sorted_tags_output.splitlines() if t.strip()]

            if sorted_tags:
                latest_tag = sorted_tags[0]
                result["latest_version"] = latest_tag

                if latest_tag != current_tag:
                    # Check if latest_tag is newer or different
                    result["status"] = "AVAILABLE"
                    result["can_update"] = True
                    # Fetch commit log between current and latest
                    try:
                        log_output = _run_git_cmd(
                            ["log", "--oneline", f"{current_tag}..{latest_tag}"], root
                        )
                        result["changelog"] = [
                            line.strip() for line in log_output.splitlines() if line.strip()
                        ]
                    except Exception:
                        result["changelog"] = [
                            f"Upgrade available from {current_tag} to {latest_tag}"
                        ]
                else:
                    result["status"] = "UNAVAILABLE"
                    result["latest_version"] = current_tag
            else:
                result["latest_version"] = current_tag
                result["status"] = "UNAVAILABLE"

        elif train == "edge":
            # Edge train tracks origin/main
            current_commit = _run_git_cmd(["rev-parse", "HEAD"], root)
            current_short = _run_git_cmd(["rev-parse", "--short", "HEAD"], root)
            result["current_version"] = current_short

            try:
                remote_commit = _run_git_cmd(["rev-parse", "origin/main"], root)
                remote_short = _run_git_cmd(["rev-parse", "--short", "origin/main"], root)
                result["latest_version"] = remote_short

                if remote_commit != current_commit:
                    result["status"] = "AVAILABLE"
                    result["can_update"] = True
                    log_output = _run_git_cmd(
                        ["log", "--oneline", f"{current_commit}..origin/main"], root
                    )
                    result["changelog"] = [
                        line.strip() for line in log_output.splitlines() if line.strip()
                    ]
                else:
                    result["status"] = "UNAVAILABLE"
            except subprocess.CalledProcessError:
                result["latest_version"] = current_short
                result["status"] = "UNAVAILABLE"

    except Exception as exc:
        result["status"] = "ERROR"
        result["error"] = str(exc)

    return result
