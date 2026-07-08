"""Cloud Run Job entrypoint for the NCAA men's volleyball pipeline.

Default behavior (no env vars set): fetch yesterday's completed games and
append them to BigQuery. This is what Cloud Scheduler triggers daily.

Men's volleyball is a spring sport (roughly January through the national
championship in early May). A daily run during the off-season (June
through December) will correctly fetch zero rows; that's expected, not a
bug - see fetch.py.

For a one-off historical backfill, set START_DATE and END_DATE (YYYY-MM-DD)
as env vars on a manual `gcloud run jobs execute` call instead of relying
on the schedule.
"""
from __future__ import annotations

import os
import sys

from bigquery_loader import ensure_dataset, get_client, load_dataframe
from fetch import fetch_boxscore_range, fetch_yesterday

SPORT = "volleyball-men"
DATASET_ID = os.environ.get("BQ_DATASET", "ncaa_volleyball_men")
TABLE_ID = os.environ.get("BQ_TABLE", "boxscores")


def main() -> None:
    start = os.environ.get("START_DATE")
    end = os.environ.get("END_DATE")

    if start and end:
        print(f"Backfilling {SPORT} {start} to {end}")
        df = fetch_boxscore_range(start, end, sport=SPORT)
    else:
        print(f"Fetching yesterday's {SPORT} boxscores")
        df = fetch_yesterday(sport=SPORT)

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
