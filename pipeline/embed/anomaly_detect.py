import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[1] / "shared"))

import os

import duckdb
import numpy as np
from ai_gateway import generate, provider_info

try:
    from sklearn.ensemble import IsolationForest
except ImportError:
    IsolationForest = None
from storage_writer import _load_env

_load_env()


def detect_anomalies(
    source: str = "truenas",
    contamination: float = 0.05,
    workspace_slug: str = "default",
    table_slug: str | None = None,
) -> str:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL vss; LOAD vss;")
    endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
    access_key = os.environ.get("MINIO_ACCESS_KEY", os.environ.get("MINIO_ROOT_USER", "admin"))
    secret_key = os.environ.get("MINIO_SECRET_KEY", os.environ.get("MINIO_ROOT_PASSWORD", "password123"))
    con.execute(f"""
        SET s3_endpoint='{endpoint}';
        SET s3_access_key_id='{access_key}';
        SET s3_secret_access_key='{secret_key}';
        SET s3_use_ssl=false; SET s3_url_style='path';
    """)

    # Only vectors from the active embedding model can be compared with each other.
    info = provider_info()
    if not info.get("dimension"):
        raise RuntimeError(
            "Cannot tell which embedding dimension to compare: is the AI provider reachable?"
        )

    clean_ws = workspace_slug.strip("/").replace("'", "''") if workspace_slug else "default"
    clean_src = (table_slug or source or "**").strip("/").replace("'", "''")
    glob = f"s3://datalake/workspaces/{clean_ws}/embeddings/{clean_src}/**/*.parquet"

    try:
        columns = {
            d[0]
            for d in con.execute(
                f"SELECT * FROM read_parquet('{glob}', union_by_name = true) LIMIT 0"
            ).description
        }
    except Exception as exc:
        if "No files found" in str(exc) or "Cannot glob" in str(exc):
            return f"No embeddings found in workspace '{clean_ws}'."
        raise

    model_filter = (
        f"AND coalesce(model, 'gemini-embedding-001') = '{info['embedding_model']}'"
        if "model" in columns
        else ""
    )
    df = con.execute(f"""
        SELECT content, embedding, source_date
        FROM read_parquet('{glob}', union_by_name = true)
        WHERE len(embedding) = {int(info["dimension"])} {model_filter}
        ORDER BY source_date DESC LIMIT 5000
    """).df()

    if df.empty:
        return f"No embeddings from the active model yet for workspace '{clean_ws}'."
    if IsolationForest is None:
        return "Isolation Forest requires scikit-learn."
    vectors = np.array(df["embedding"].tolist())
    clf = IsolationForest(contamination=contamination, random_state=42)
    df["anomaly"] = clf.fit_predict(vectors)

    anomalies = df[df["anomaly"] == -1]["content"].tolist()
    if not anomalies:
        return "No anomalies detected."

    return generate(
        f"The following {clean_src} entries in workspace '{clean_ws}' were flagged as anomalies. "
        f"Summarize what's unusual about them:\n\n" + "\n".join(anomalies[:20])
    )


if __name__ == "__main__":
    print(detect_anomalies())
