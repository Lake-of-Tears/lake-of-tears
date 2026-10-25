from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    from pipeline.shared.toon import parse_toon
except ImportError:
    try:
        from ui.toon import parse_toon
    except ImportError:
        from toon import parse_toon

logger = logging.getLogger("lake.connector_registry")

def _find_connectors_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parent.parent / "connectors",
        Path(__file__).resolve().parent / "connectors",
        Path(__file__).resolve().parent.parent / "pipeline" / "connectors",
        Path("/app/connectors"),
        Path("/app/pipeline/connectors"),
    ]
    for c in candidates:
        if c.exists() and any(c.glob("*/connector.toon")):
            return c
    return candidates[0]


DEFAULT_CONNECTORS_DIR = _find_connectors_dir()


class ConnectorRegistry:
    """Registry that auto-discovers and manages community connectors defined in TOON format."""

    def __init__(self, connectors_dir: Path | str | None = None):
        self.connectors_dir = Path(connectors_dir) if connectors_dir else DEFAULT_CONNECTORS_DIR
        self._cache: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """Scan connectors directory and load all connector.toon files."""
        self._cache.clear()
        if not self.connectors_dir.exists():
            return

        for p in self.connectors_dir.glob("*/connector.toon"):
            try:
                content = p.read_text(encoding="utf-8")
                manifest = parse_toon(content)
                cid = manifest.get("id") or p.parent.name
                manifest["id"] = cid
                manifest["folder"] = p.parent.name
                manifest["manifest_path"] = str(p)
                self._cache[cid] = manifest
            except Exception as exc:
                logger.error("Failed to parse TOON connector manifest at %s: %s", p, exc)

    def list_connectors(self) -> list[dict[str, Any]]:
        """Return all discovered connectors sorted by category and name."""
        return sorted(
            self._cache.values(),
            key=lambda c: (c.get("category", "General"), c.get("name", "")),
        )

    def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        return self._cache.get(connector_id)

    def get_categories(self) -> list[str]:
        """Return sorted list of unique connector categories."""
        cats = {c.get("category", "General") for c in self._cache.values()}
        return sorted(cats)

    def resolve_workspace_feeds(
        self,
        workspace_slug: str | None,
        bucket_stats: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify connectors into active workspace feeds and the community marketplace.

        Returns:
            {
                "active_feeds": [...],
                "community_connectors": [...],
                "categories": [...],
                "total_count": int,
            }
        """
        clean_slug = (workspace_slug or "default").strip("/")
        sources_by_key = (
            {s["key"]: s for s in bucket_stats.get("sources", [])}
            if bucket_stats.get("ok")
            else {}
        )

        active_feeds: list[dict[str, Any]] = []
        community_connectors: list[dict[str, Any]] = []

        all_connectors = self.list_connectors()

        for conn in all_connectors:
            cid = conn["id"]
            src_stat = sources_by_key.get(cid, {})

            # Check if source has landed data or sync status in bucket stats
            last_mod_str = src_stat.get("last_modified_str", "Never")
            status = src_stat.get("status", "red")
            has_data = status in ("green", "yellow") or last_mod_str != "Never"

            dag_id = conn.get("dag_id") or f"ingest_{cid}"
            landing_path = f"workspaces/{clean_slug}/raw/{cid}/"

            enriched = {
                **conn,
                "id": cid,
                "key": cid,
                "dag_id": dag_id,
                "status": status,
                "status_label": "Synced"
                if status == "green"
                else ("Stale" if status == "yellow" else "No data"),
                "last_modified_str": last_mod_str,
                "landing_path": landing_path,
                "has_data": has_data,
            }

            if has_data:
                active_feeds.append(enriched)

            community_connectors.append(enriched)

        return {
            "active_feeds": active_feeds,
            "community_connectors": community_connectors,
            "categories": self.get_categories(),
            "total_count": len(all_connectors),
        }


# Global registry singleton
registry = ConnectorRegistry()
