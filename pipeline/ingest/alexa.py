import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[2]))
from storage_writer import write_to_storage

EXPORT_DIR = "/home/openclaw/alexa-exports"


def parse_alexa_export(export_dir: str) -> pd.DataFrame:
    records = []
    for f in Path(export_dir).glob("*.json"):
        data = json.loads(f.read_text())
        if isinstance(data, list):
            records.extend(data)
        elif isinstance(data, dict):
            records.append(data)
    return pd.json_normalize(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Alexa smart home data into datalake")
    parser.add_argument(
        "--workspace",
        default=os.getenv("WORKSPACE_SLUG", os.getenv("WORKSPACE", "default")),
        help="Target workspace slug (default: default)",
    )
    parser.add_argument(
        "--date",
        default=str(date.today()),
        help="Target date YYYY-MM-DD (defaults to today)",
    )
    args, _ = parser.parse_known_args()

    df = parse_alexa_export(EXPORT_DIR)
    write_to_storage(df, "alexa", args.date, workspace=args.workspace)
