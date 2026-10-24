import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parents[2]))
from storage_writer import write_to_storage

# State College, PA — override via env if needed
LATITUDE = float(os.environ.get("WEATHER_LAT", "40.7934"))
LONGITUDE = float(os.environ.get("WEATHER_LON", "-77.8600"))

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather(lat: float, lon: float, target_date: str) -> pd.DataFrame:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation",
                "wind_speed_10m",
                "wind_direction_10m",
                "apparent_temperature",
                "cloud_cover",
            ]
        ),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "America/New_York",
        "start_date": target_date,
        "end_date": target_date,
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    hourly = data["hourly"]
    df = pd.DataFrame(hourly)
    df.rename(columns={"time": "timestamp"}, inplace=True)
    df["source_date"] = target_date
    df["latitude"] = lat
    df["longitude"] = lon
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest weather data into datalake")
    parser.add_argument(
        "--workspace",
        default=os.getenv("WORKSPACE_SLUG", os.getenv("WORKSPACE", "default")),
        help="Target workspace slug (default: default)",
    )
    parser.add_argument(
        "--date",
        default=str(date.today() - timedelta(days=1)),
        help="Target date YYYY-MM-DD (defaults to yesterday)",
    )
    args, _ = parser.parse_known_args()

    df = fetch_weather(LATITUDE, LONGITUDE, args.date)
    write_to_storage(df, "weather", args.date, workspace=args.workspace)
