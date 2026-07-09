"""Cloud Run Job entrypoint for the MLV (Major League Volleyball) pipeline.

Default behavior (no env vars set): fetch every completed match across
every season the API knows about, skip whatever's already in BigQuery, and
append the rest. Unlike the NCAA/baseball/hockey pipelines there's no
"yesterday" date window here - MLV plays 2-4 games a week, not most days -
so incremental-ness comes from comparing against what's already loaded
(bigquery_loader.get_loaded_match_ids) rather than a date filter. That
also means a full backfill and a normal scheduled run are the exact same
code path: the very first run loads the whole 2025-26 season, and every
run after that just picks up whatever's new.

Set SEASON_ID to restrict a run to one season (rarely needed - mostly
useful for re-running a single season's backfill in isolation).
"""
from __future__ import annotations

import os
import sys

from bigquery_loader import ensure_dataset, get_client, get_loaded_match_ids, load_dataframe
from fetch import fetch_new_matches

DATASET_ID = os.environ.get("BQ_DATASET", "mlv_volleyball")
MATCHES_TABLE = os.environ.get("BQ_MATCHES_TABLE", "matches")
BOXSCORES_TABLE = os.environ.get("BQ_BOXSCORES_TABLE", "boxscores")


def main() -> None:
    season_id_env = os.environ.get("SEASON_ID")
    season_ids = [int(season_id_env)] if season_id_env else None

    client = get_client()
    ensure_dataset(client, DATASET_ID)

    already_loaded = get_loaded_match_ids(client, DATASET_ID, MATCHES_TABLE)
    print(f"{len(already_loaded)} matches already in BigQuery")

    matches_df, boxscores_df = fetch_new_matches(
        season_ids=season_ids, already_loaded_match_ids=already_loaded
    )
    print(
        f"Fetched {len(matches_df)} new matches, "
        f"{len(boxscores_df)} new player boxscore rows"
    )

    load_dataframe(client, matches_df, DATASET_ID, MATCHES_TABLE)
    load_dataframe(client, boxscores_df, DATASET_ID, BOXSCORES_TABLE)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - surface any failure to Cloud Run logs
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
