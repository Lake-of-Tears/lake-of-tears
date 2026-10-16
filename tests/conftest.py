from __future__ import annotations

import sys
from pathlib import Path

repo_root = Path(__file__).parents[1]
ui_dir = str(repo_root / "ui")
backend_dir = str(repo_root / "backend")

sys.path.insert(0, ui_dir)
sys.path.insert(0, backend_dir)
sys.path.insert(0, str(repo_root))

# Register ui modules into sys.modules so imports of both 'ui.X' and 'X' point to the same module
import ui.duckdb_ai  # noqa: E402
import ui.duckdb_pool  # noqa: E402
import ui.main  # noqa: E402
import ui.notebook_proxy  # noqa: E402

sys.modules["main"] = ui.main
sys.modules["notebook_proxy"] = ui.notebook_proxy
sys.modules["duckdb_ai"] = ui.duckdb_ai
sys.modules["duckdb_pool"] = ui.duckdb_pool
