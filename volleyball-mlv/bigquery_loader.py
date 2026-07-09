"""Load MLV match and boxscore DataFrames into BigQuery.

Same append/autodetect pattern as the other pipelines, plus one addition:
get_loaded_match_ids(), used to dedupe against matches already loaded on a
previous run. MLV has no natural daily cutoff the way the NCAA pipelines
get one from a "yesterday" date filter (games happen on an uneven
2-4-per-week schedule, not most days), so instead this pipeline tracks
which volley_station_match_id values are already in BigQuery and skips
them on every run - see fetch.fetch_new_matches.
"""
from __future__ import annotations

import pandas as pd
from google.cloud import bigquery


def get_client(project_id: str | None = None) -> bigquery.Client:
    """Create a BigQuery client. On Cloud Run this picks up the job's
    attached service account automatically; project_id can be left None."""
    return bigquery.Client(project=project_id)


def ensure_dataset(client: bigquery.Client, dataset_id: str, location: str = "US") -> None:
    """Create the dataset if it doesn't already exist. Idempotent."""
    dataset_ref = bigquery.DatasetReference(client.project, dataset_id)
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = location
        client.create_dataset(dataset)
        print(f"Created dataset {client.project}.{dataset_id}")


def get_loaded_match_ids(
    client: bigquery.Client, dataset_id: str, table_id: str = "matches"
) -> set[int]:
    """volley_station_match_id values already in BigQuery, so a re-run
    doesn't reload the same match twice. Returns an empty set (not an
    error) if the table doesn't exist yet - that just means this is the
    first run."""
    table_ref = f"{client.project}.{dataset_id}.{table_id}"
    try:
        query = f"SELECT DISTINCT volley_station_match_id FROM `{table_ref}`"
        return {row.volley_station_match_id for row in client.query(query).result()}
    except Exception as exc:
        print(f"No existing matches table found ({exc}); treating as first run.")
        return set()


def load_dataframe(
    client: bigquery.Client,
    df: pd.DataFrame,
    dataset_id: str,
    table_id: str,
) -> None:
    """Append a DataFrame of rows into the target table."""
    if df.empty:
        print(f"No rows to load into {table_id}; skipping.")
        return

    table_ref = f"{client.project}.{dataset_id}.{table_id}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        autodetect=True,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()  # wait for completion, raises on failure
    print(f"Loaded {len(df)} rows into {table_ref}")
