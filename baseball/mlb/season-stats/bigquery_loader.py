"""Load MLB Stats API season snapshots into BigQuery.

Each row is a player's (or team's) cumulative season-to-date stat line as
of a given day, stamped with snapshot_date. Every daily run appends a new
snapshot rather than replacing the table, so the table builds up a history
of how the leaderboards moved over the season - useful for trending things
like "how did the batting average leaders shift heading into the All-Star
Game." Re-running the job on the same day is still safe and idempotent:
before appending, any existing rows for that day's snapshot_date get
deleted first, so a manual re-run replaces that day's snapshot instead of
duplicating it.

Downstream queries that only want "the season as it stands right now"
should filter to the latest snapshot_date (or use the *_latest views, if
those have been created - see DEPLOY.md) rather than reading the whole
table.
"""
from __future__ import annotations

import re

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


def _sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """MLB Stats API field names are mostly clean camelCase already, but
    sanitize anyway as cheap insurance against BigQuery-invalid characters
    and column names that start with a digit."""
    def clean(col: str) -> str:
        col = re.sub(r"[^0-9a-zA-Z_]", "_", str(col))
        if col[0].isdigit():
            col = f"_{col}"
        return col

    df = df.rename(columns={c: clean(c) for c in df.columns})
    seen: dict[str, int] = {}
    new_columns = []
    for col in df.columns:
        if col in seen:
            seen[col] += 1
            new_columns.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            new_columns.append(col)
    df.columns = new_columns
    return df


def _delete_existing_snapshot(client: bigquery.Client, table_ref: str, snapshot_date: str) -> None:
    """Remove any rows already loaded for this snapshot_date before
    appending fresh ones. Makes re-running the job on the same day
    idempotent - it replaces that day's snapshot instead of appending a
    duplicate copy of it."""
    query = f"DELETE FROM `{table_ref}` WHERE snapshot_date = @snapshot_date"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("snapshot_date", "STRING", snapshot_date)]
    )
    try:
        client.query(query, job_config=job_config).result()
    except Exception as exc:
        # Most likely the table doesn't exist yet (first-ever load) - treat
        # as nothing to delete rather than failing the whole run.
        print(f"Delete-existing-snapshot skipped (table probably doesn't exist yet): {exc}")


def append_snapshot(
    client: bigquery.Client,
    df: pd.DataFrame,
    dataset_id: str,
    table_id: str,
    snapshot_date: str,
) -> None:
    """Append this DataFrame as today's snapshot, stamped with
    snapshot_date. Deletes any existing rows for the same snapshot_date
    first, so this is safe to re-run on the same day."""
    if df.empty:
        print(f"No rows fetched for {table_id}; leaving existing table as-is.")
        return

    df = _sanitize_columns(df)
    df["snapshot_date"] = snapshot_date

    table_ref = f"{client.project}.{dataset_id}.{table_id}"
    _delete_existing_snapshot(client, table_ref, snapshot_date)

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        autodetect=True,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()  # wait for completion, raises on failure
    print(f"Appended {len(df)} rows into {table_ref} (snapshot_date={snapshot_date})")
