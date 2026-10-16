import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[1] / "shared"))

from datetime import date, timedelta

import pandas as pd
from gemini_embedder import get_gemini_client, get_gemini_default_model
from storage_writer import get_duckdb_con, write_to_storage

SOURCES = {
    "jellyfin": ("Name", "raw/jellyfin/**/*.parquet"),
    "truenas": ("name", "raw/truenas/**/*.parquet"),
    "weather": ("temperature_2m", "raw/weather/**/*.parquet"),
}


def summarize_day(source: str, text_col: str, parquet_glob: str):
    yesterday = str(date.today() - timedelta(days=1))
    con = get_duckdb_con()

    df = con.execute(f"""
        SELECT {text_col} FROM read_parquet('s3://datalake/{parquet_glob}')
        WHERE source_date = '{yesterday}'
        LIMIT 200
    """).df()

    if df.empty:
        return

    client = get_gemini_client()
    content = "\n".join(df[text_col].dropna().astype(str).tolist())
    response = client.models.generate_content(
        model=get_gemini_default_model(),
        contents=f"Summarize the following {source} activity from {yesterday} "
        f"in 3-5 bullet points:\n\n{content}",
    )
    summary = response.text

    summary_df = pd.DataFrame([{"source": source, "date": yesterday, "summary": summary}])
    write_to_storage(summary_df, f"summaries/{source}", yesterday)


if __name__ == "__main__":
    for source, (col, glob) in SOURCES.items():
        summarize_day(source, col, glob)
