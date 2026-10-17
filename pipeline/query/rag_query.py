import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[1] / "shared"))

import os

import duckdb
from ai_gateway import embed_texts_with_meta, generate, vector_search_sql

EMBEDDINGS_GLOB = "s3://datalake/embeddings/**/*.parquet"


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        SET s3_endpoint='{os.environ["MINIO_ENDPOINT"]}';
        SET s3_access_key_id='{os.environ["MINIO_ACCESS_KEY"]}';
        SET s3_secret_access_key='{os.environ["MINIO_SECRET_KEY"]}';
        SET s3_use_ssl=false; SET s3_url_style='path';
    """)
    return con


def rag(question: str, top_k: int = 10) -> str:
    # Embed the question with the active provider; its dimension and model decide which stored
    # vectors can be compared (older files without a "model" column count as the legacy Gemini model).
    emb = embed_texts_with_meta([question], query=True)
    query_vec, dimension, model = emb["embeddings"][0], emb["dimension"], emb["embedding_model"]

    con = _connect()
    columns = {
        d[0]
        for d in con.execute(
            f"SELECT * FROM read_parquet('{EMBEDDINGS_GLOB}', union_by_name = true) LIMIT 0"
        ).description
    }
    has_model = "model" in columns
    params: list = [query_vec, EMBEDDINGS_GLOB] + ([model] if has_model else []) + [top_k]
    results = con.execute(vector_search_sql(dimension, has_model), params).df()

    context = "\n".join(f"[{r.source}] {r.content}" for _, r in results.iterrows())
    return generate(
        "You are an assistant that answers questions about home data.\n\n"
        f"Context from data lake:\n{context}\n\n"
        f"Question: {question}"
    )


if __name__ == "__main__":
    print(rag(" ".join(sys.argv[1:])))
