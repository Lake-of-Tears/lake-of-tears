"""
Pulls TrueNAS system logs for yesterday and writes them to the datalake.

Two sources:
  1. systemd journal (WARNING and above) via SSH — catches disk errors, ZFS events,
     middleware faults, SMART failures, network issues, etc.
  2. Active alerts from the TrueNAS REST API — pool degraded, low space, etc.

Output: s3://datalake/raw/truenas_logs/year=YYYY/month=MM/day=DD/data.parquet

Runs at 00:00 daily so it captures the day that just ended.
SSH key (root@TRUENAS_HOST) must already be in place on the VM.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parents[2]))
from storage_writer import _load_env, write_to_storage

_load_env()

TRUENAS_HOST = os.environ["TRUENAS_HOST"]
TRUENAS_API_KEY = os.environ["TRUENAS_API_KEY"]

TODAY = str(date.today())
YESTERDAY = str(date.today() - timedelta(days=1))

PRIORITY_NAMES = {
    "0": "EMERGENCY",
    "1": "ALERT",
    "2": "CRITICAL",
    "3": "ERROR",
    "4": "WARNING",
}


def fetch_journal_logs():
    """Pull WARNING+ journal entries for yesterday via SSH."""
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        f"root@{TRUENAS_HOST}",
        (
            f"journalctl --since '{YESTERDAY} 00:00:00' "
            f"--until '{TODAY} 00:00:00' "
            f"-p warning --no-pager -o json 2>/dev/null"
        ),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("journalctl SSH timed out — skipping journal logs")
        return []

    entries = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue

        priority = str(e.get("PRIORITY", "4"))
        unit = e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "kernel"
        unit = unit.replace(".service", "")
        message = e.get("MESSAGE", "")
        if not message:
            continue

        # Convert microsecond epoch to ISO timestamp
        ts_us = e.get("__REALTIME_TIMESTAMP")
        try:
            import datetime

            ts = datetime.datetime.fromtimestamp(int(ts_us) / 1e6).isoformat()
        except Exception:
            ts = YESTERDAY

        entries.append(
            {
                "log_type": "journal",
                "unit": unit,
                "priority": PRIORITY_NAMES.get(priority, priority),
                "timestamp": ts,
                "message": message,
            }
        )

    print(f"  Journal entries (WARNING+): {len(entries)}")
    return entries


def fetch_alerts():
    """Pull active alerts from TrueNAS REST API."""
    try:
        resp = requests.get(
            f"http://{TRUENAS_HOST}/api/v2.0/alert/list",
            headers={"Authorization": f"Bearer {TRUENAS_API_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  Alerts fetch failed: {e}")
        return []

    entries = []
    for a in resp.json():
        text = a.get("formatted") or a.get("text") or str(a.get("args", ""))
        if not text:
            continue

        ts_ms = a.get("datetime", {})
        if isinstance(ts_ms, dict):
            ts_ms = ts_ms.get("$date", 0)
        try:
            import datetime

            ts = datetime.datetime.fromtimestamp(int(ts_ms) / 1000).isoformat()
        except Exception:
            ts = YESTERDAY

        entries.append(
            {
                "log_type": "alert",
                "unit": a.get("source", "alert"),
                "priority": a.get("level", "WARNING"),
                "timestamp": ts,
                "message": text,
            }
        )

    print(f"  Active alerts: {len(entries)}")
    return entries


def main(workspace: str | None = None, target_date: str | None = None):
    chosen_date = target_date or YESTERDAY
    print(f"Ingesting TrueNAS logs for {chosen_date}")

    journal = fetch_journal_logs()
    alerts = fetch_alerts()
    all_entries = journal + alerts

    if not all_entries:
        print("No log entries found — nothing to write")
        return

    df = pd.DataFrame(all_entries)
    df["content"] = df.apply(lambda r: f"[{r['unit']}] {r['priority']}: {r['message']}", axis=1)
    df["id"] = df["content"].apply(
        lambda t: hashlib.md5(t.encode(), usedforsecurity=False).hexdigest()
    )
    df["source_date"] = chosen_date
    df["ingested_at"] = pd.Timestamp.now()

    target_ws = workspace or os.getenv("WORKSPACE_SLUG", os.getenv("WORKSPACE", "default"))
    path = write_to_storage(df, "truenas_logs", chosen_date, workspace=target_ws)
    print(f"Wrote {len(df)} log entries → {path} ({chosen_date})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest TrueNAS system logs into datalake")
    parser.add_argument(
        "--workspace",
        default=os.getenv("WORKSPACE_SLUG", os.getenv("WORKSPACE", "default")),
        help="Target workspace slug (default: default)",
    )
    parser.add_argument(
        "--date",
        default=YESTERDAY,
        help="Target date YYYY-MM-DD (defaults to yesterday)",
    )
    args, _ = parser.parse_known_args()
    main(workspace=args.workspace, target_date=args.date)
