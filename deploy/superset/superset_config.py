# Superset configuration for Lake of Tears
# Mounted at /app/pythonpath/superset_config.py inside the container.

# Serve Superset under /superset/ path (proxied by nginx).
APPLICATION_PREFIX_PATH = "/superset"

# Feature flags: Enable native embedded dashboards
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
}

# Role for guest token users
GUEST_ROLE_NAME = "Gamma"

# Allow Superset to be embedded in an iframe from the Lake UI shell.
# Default HTTP_HEADERS includes X-Frame-Options: SAMEORIGIN which blocks iframes.
HTTP_HEADERS = {}
# Flask-Talisman configuration for embedding
TALISMAN_CONFIG = {
    "frame_options": "ALLOWALL",
    "content_security_policy": None,
}

# Grant anonymous users Gamma-level read access
PUBLIC_ROLE_LIKE = "Gamma"
