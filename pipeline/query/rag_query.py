import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[1] / "shared"))

import os

import duckdb
from gemini_embedder import embed_query, get_gemini_client, get_gemini_default_model


def rag(question: str, top_k: int = 10) -> str:
    query_vec = embed_query(question)

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL vss; LOAD vss;")
    con.execute(f"""
        SET s3_endpoint='{os.environ["MINIO_ENDPOINT"]}';
        SET s3_access_key_id='{os.environ["MINIO_ACCESS_KEY"]}';
        SET s3_secret_access_key='{os.environ["MINIO_SECRET_KEY"]}';
        SET s3_use_ssl=false; SET s3_url_style='path';
    """)

    results = con.execute(f"""
        SELECT source, content,
               array_cosine_similarity(embedding::DOUBLE[768], {query_vec}::DOUBLE[768]) AS score
        FROM read_parquet('s3://datalake/embeddings/**/*.parquet')
        ORDER BY score DESC
        LIMIT {top_k}
    """).df()

    context = "\n".join(f"[{r.source}] {r.content}" for _, r in results.iterrows())

    client = get_gemini_client()
    response = client.models.generate_content(
        model=get_gemini_default_model(),
        contents=f"You are an assistant that answers questions about home data.\n\n"
        f"Context from data lake:\n{context}\n\n"
        f"Question: {question}",
    )
    return response.text


if __name__ == "__main__":
    print(rag(" ".join(sys.argv[1:])))
