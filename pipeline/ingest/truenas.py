import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parents[2]))
from storage_writer import write_to_storage

TRUENAS_URL = f"http://{os.environ.get('TRUENAS_HOST', '')}/api/v2.0"
API_KEY = os.environ.get("TRUENAS_API_KEY", "")


def fetch_pool_stats() -> pd.DataFrame:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    pools = requests.get(f"{TRUENAS_URL}/pool", headers=headers, timeout=30)
    pools.raise_for_status()
    return pd.json_normalize(pools.json())


def fetch_disk_info() -> pd.DataFrame:
    """Fetch disk inventory + SMART status via TrueNAS REST API."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        disks = requests.get(f"{TRUENAS_URL}/disk", headers=headers, timeout=30)
        disks.raise_for_status()
        rows = []
        for d in disks.json():
            rows.append(
                {
                    "devname": d.get("devname", ""),
                    "serial": d.get("serial", ""),
                    "model": d.get("model", ""),
                    "size": d.get("size", 0),
                    "rotationrate": d.get("rotationrate"),
                    "description": d.get("description", ""),
                }
            )
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"[ingest_truenas] disk info failed: {e}", file=sys.stderr)
        return pd.DataFrame()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest TrueNAS pool stats and disk info into datalake"
    )
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

    pool_df = fetch_pool_stats()
    pool_df["source_date"] = args.date
    write_to_storage(pool_df, "truenas", args.date, workspace=args.workspace)
    disk_df = fetch_disk_info()
    if not disk_df.empty:
        disk_df["source_date"] = args.date
        write_to_storage(disk_df, "truenas_disks", args.date, workspace=args.workspace)
