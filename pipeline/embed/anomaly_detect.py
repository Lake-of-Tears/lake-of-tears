import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[1] / "shared"))

import os

import duckdb
import numpy as np
from ai_gateway import generate, provider_info
from sklearn.ensemble import IsolationForest
from storage_writer import _load_env

_load_env()


def detect_anomalies(source: str = "truenas", contamination: float = 0.05) -> str:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL vss; LOAD vss;")
    con.execute(f"""
        SET s3_endpoint='{os.environ["MINIO_ENDPOINT"]}';
        SET s3_access_key_id='{os.environ["MINIO_ACCESS_KEY"]}';
        SET s3_secret_access_key='{os.environ["MINIO_SECRET_KEY"]}';
        SET s3_use_ssl=false; SET s3_url_style='path';
    """)

    # Only vectors from the active embedding model can be compared with each other.
    info = provider_info()
    glob = f"s3://datalake/embeddings/{source}/**/*.parquet"
    columns = {
        d[0]
        for d in con.execute(
            f"SELECT * FROM read_parquet('{glob}', union_by_name = true) LIMIT 0"
        ).description
    }
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
        return "No embeddings from the active model yet."
    vectors = np.array(df["embedding"].tolist())
    clf = IsolationForest(contamination=contamination, random_state=42)
    df["anomaly"] = clf.fit_predict(vectors)

    anomalies = df[df["anomaly"] == -1]["content"].tolist()
    if not anomalies:
        return "No anomalies detected."

    return generate(
        f"The following {source} entries were flagged as anomalies. "
        f"Summarize what's unusual about them:\n\n" + "\n".join(anomalies[:20])
    )


if __name__ == "__main__":
    print(detect_anomalies())
