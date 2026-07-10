"""Cloud Run Job entrypoint.

Default behavior (no env vars set): fetch yesterday's Statcast data and
append it to BigQuery. This is what Cloud Scheduler triggers daily.

For a one-off historical backfill, set START_DATE and END_DATE (YYYY-MM-DD)
as env vars on a manual `gcloud run jobs execute` call instead of relying
on the schedule.
"""
from __future__ import annotations

import os
import sys

from bigquery_loader import ensure_dataset, get_client, load_dataframe
from fetch import fetch_statcast_range, fetch_recent

DATASET_ID = os.environ.get("BQ_DATASET", "mlb_statcast")
TABLE_ID = os.environ.get("BQ_TABLE", "pitches")


def main() -> None:
    start = os.environ.get("START_DATE")
    end = os.environ.get("END_DATE")

    if start and end:
        print(f"Backfilling {start} to {end}")
        df = fetch_statcast_range(start, end)
    else:
        print("Fetching recent Statcast data (rolling window)")
        df = fetch_recent()

    print(f"Fetched {len(df)} rows")

    client = get_client()
    ensure_dataset(client, DATASET_ID)
    load_dataframe(client, df, DATASET_ID, TABLE_ID)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - surface any failure to Cloud Run logs
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
