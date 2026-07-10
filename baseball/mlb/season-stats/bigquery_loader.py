"""Load MLB Stats API season snapshots into BigQuery.

Unlike the Statcast pitch-level pipeline, this data isn't an append-only
event log - a player's season stat line changes every day as they play more
games. So instead of appending and deduping by date, each daily run replaces
the table outright (WRITE_TRUNCATE) with the latest full-season snapshot.
This is "the season as it stands right now," refreshed once a day, not a
history of every day's numbers.
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


def load_snapshot(
    client: bigquery.Client,
    df: pd.DataFrame,
    dataset_id: str,
    table_id: str,
    snapshot_date: str,
) -> None:
    """Replace the target table with this DataFrame, stamped with the date
    of this run so it's clear how fresh the snapshot is."""
    if df.empty:
        print(f"No rows fetched for {table_id}; leaving existing table as-is.")
        return

    df = _sanitize_columns(df)
    df["snapshot_date"] = snapshot_date

    table_ref = f"{client.project}.{dataset_id}.{table_id}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        autodetect=True,
    )
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()  # wait for completion, raises on failure
    print(f"Loaded {len(df)} rows into {table_ref} (snapshot_date={snapshot_date})")
