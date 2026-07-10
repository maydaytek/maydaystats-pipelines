"""Cloud Run Job entrypoint.

Fetches four MLB Stats API season leaderboards - batting, pitching, team
batting, team pitching - for SEASON (defaults to the current year) and
replaces each of their BigQuery tables with the latest snapshot. Meant to
run once a day; there's no "yesterday" concept here like the other
pipelines, since these are cumulative season stats, not per-day events.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

from bigquery_loader import ensure_dataset, get_client, load_snapshot
from fetch import fetch_batting, fetch_pitching, fetch_team_batting, fetch_team_pitching

DATASET_ID = os.environ.get("BQ_DATASET", "mlb_season_stats")
SEASON = int(os.environ.get("SEASON", dt.date.today().year))

TABLES = {
    "batting": fetch_batting,
    "pitching": fetch_pitching,
    "team_batting": fetch_team_batting,
    "team_pitching": fetch_team_pitching,
}


def main() -> None:
    print(f"Fetching {SEASON} MLB Stats API season snapshots")

    client = get_client()
    ensure_dataset(client, DATASET_ID)
    snapshot_date = dt.date.today().isoformat()

    failures = []
    for table_id, fetch_fn in TABLES.items():
        try:
            df = fetch_fn(SEASON)
            print(f"{table_id}: fetched {len(df)} rows")
            load_snapshot(client, df, DATASET_ID, table_id, snapshot_date)
        except Exception as exc:  # noqa: BLE001 - keep going, report all failures at the end
            print(f"{table_id}: failed - {exc}", file=sys.stderr)
            failures.append(table_id)

    if failures:
        raise RuntimeError(f"Failed to refresh: {', '.join(failures)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - surface any failure to Cloud Run logs
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
