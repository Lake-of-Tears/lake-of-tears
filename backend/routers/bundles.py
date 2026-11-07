"""Lake Asset Bundles (LABs) router re-export for backend.routers package."""

try:
    from backend.bundles.router import router
except ImportError:
    from bundles.router import router

__all__ = ["router"]
