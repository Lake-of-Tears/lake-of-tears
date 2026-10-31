#!/usr/bin/env python3
"""CLI utility to compile Lake of Tears TOON specifications into Helm values.yaml."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path for direct script execution
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.shared.toon2helm import main  # noqa: E402

if __name__ == "__main__":
    main()
