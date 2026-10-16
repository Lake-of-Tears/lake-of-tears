import json
import os
import pathlib
import urllib.request

from google import genai


def _load_env():
    for env_path in (
        pathlib.Path(__file__).parent / ".env",
        pathlib.Path(__file__).parents[2] / ".env",
    ):
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())


_load_env()


def get_gemini_api_key() -> str:
    """Retrieve the active GEMINI_API_KEY configured in the Lake of Tears Admin Panel."""
    backend_url = os.getenv("BACKEND_URL", "http://backend:8000").rstrip("/")
    internal_secret = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
    req = urllib.request.Request(
        f"{backend_url}/api/internal/settings/gemini-key",
        headers={"X-Internal-Secret": internal_secret},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
            payload = json.loads(resp.read().decode("utf-8"))
            key = str(payload.get("gemini_api_key") or "").strip()
            if key:
                return key
    except Exception:
        pass
    raise RuntimeError(
        "GEMINI_API_KEY is not set. A superadmin can configure it in Settings → Admin Panel."
    )


def get_gemini_default_model() -> str:
    """Retrieve the default Gemini model configured in the Lake of Tears Admin Panel."""
    backend_url = os.getenv("BACKEND_URL", "http://backend:8000").rstrip("/")
    internal_secret = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
    req = urllib.request.Request(
        f"{backend_url}/api/internal/settings/gemini-key",
        headers={"X-Internal-Secret": internal_secret},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
            payload = json.loads(resp.read().decode("utf-8"))
            model = str(payload.get("gemini_default_model") or "").strip()
            if model:
                return model
    except Exception:
        pass
    return "gemini-3.8-flash"


def get_gemini_client() -> genai.Client:
    """Create a Google GenAI client using the Admin Panel GEMINI_API_KEY."""
    return genai.Client(api_key=get_gemini_api_key())


def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    client = get_gemini_client()
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=texts,
        config={"task_type": task_type, "output_dimensionality": 768},
    )
    return [e.values for e in result.embeddings]


def embed_query(text: str) -> list[float]:
    client = get_gemini_client()
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=[text],
        config={"task_type": "RETRIEVAL_QUERY", "output_dimensionality": 768},
    )
    return result.embeddings[0].values
