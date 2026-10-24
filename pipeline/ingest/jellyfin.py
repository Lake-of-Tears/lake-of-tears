import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parents[2]))
from storage_writer import write_to_storage

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "")
API_KEY = os.environ.get("JELLYFIN_API_KEY", "")


def fetch_activity() -> pd.DataFrame:
    resp = requests.get(
        f"{JELLYFIN_URL}/System/ActivityLog/Entries",
        headers={"X-Emby-Token": API_KEY},
        params={"limit": 1000},
        timeout=30,
    )
    resp.raise_for_status()
    return pd.DataFrame(resp.json().get("Items", []))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Jellyfin activity log into datalake")
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

    df = fetch_activity()
    df["source_date"] = args.date
    write_to_storage(df, "jellyfin", args.date, workspace=args.workspace)
