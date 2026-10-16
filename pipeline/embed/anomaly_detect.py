import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[1] / "shared"))

import os

import duckdb
import numpy as np
from gemini_embedder import get_gemini_client
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

    df = con.execute(f"""
        SELECT content, embedding, source_date
        FROM read_parquet('s3://datalake/embeddings/{source}/**/*.parquet')
        ORDER BY source_date DESC LIMIT 5000
    """).df()

    vectors = np.array(df["embedding"].tolist())
    clf = IsolationForest(contamination=contamination, random_state=42)
    df["anomaly"] = clf.fit_predict(vectors)

    anomalies = df[df["anomaly"] == -1]["content"].tolist()
    if not anomalies:
        return "No anomalies detected."

    client = get_gemini_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"The following {source} entries were flagged as anomalies. "
        f"Summarize what's unusual about them:\n\n" + "\n".join(anomalies[:20]),
    )
    return response.text


if __name__ == "__main__":
    print(detect_anomalies())
