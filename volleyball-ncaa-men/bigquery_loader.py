"""Load a boxscore DataFrame into BigQuery, creating the dataset if needed.

Identical pattern to the baseball and hockey pipelines' loaders: append,
autodetect schema, allow new columns.
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


def _drop_already_loaded(
    client: bigquery.Client, df: pd.DataFrame, table_ref: str, date_column: str
) -> pd.DataFrame:
    """Filter out rows whose date_column value is already present in the
    target table. Needed because fetch_recent() pulls a rolling window
    that deliberately overlaps with previous runs (self-healing against a
    source that hadn't published a prior day's data yet) - without this,
    every overlapping day would get double-loaded on each run."""
    dates = sorted(df[date_column].dropna().unique().tolist())
    if not dates:
        return df

    query = f"SELECT DISTINCT {date_column} FROM `{table_ref}` WHERE {date_column} IN UNNEST(@dates)"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("dates", "STRING", dates)]
    )
    try:
        existing = {row[date_column] for row in client.query(query, job_config=job_config).result()}
    except Exception as exc:
        # Most likely the table doesn't exist yet (first-ever load) - treat
        # as nothing loaded rather than failing the whole run.
        print(f"Dedup check skipped (table probably doesn't exist yet): {exc}")
        return df

    if not existing:
        return df

    before = len(df)
    df = df[~df[date_column].isin(existing)]
    skipped = before - len(df)
    if skipped:
        print(f"Skipping {skipped} rows already loaded for dates: {sorted(existing)}")
    return df


def load_dataframe(
    client: bigquery.Client,
    df: pd.DataFrame,
    dataset_id: str,
    table_id: str,
    date_column: str = "game_date",
) -> None:
    """Append a DataFrame of boxscore rows into the target table, skipping
    any rows for dates already loaded (see _drop_already_loaded)."""
    if df.empty:
        print("No rows to load; skipping.")
        return

    table_ref = f"{client.project}.{dataset_id}.{table_id}"

    if date_column in df.columns:
        df = _drop_already_loaded(client, df, table_ref, date_column)
        if df.empty:
            print("No new rows to load after dedup; skipping.")
            return

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        autodetect=True,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()  # wait for completion, raises on failure
    print(f"Loaded {len(df)} rows into {table_ref}")
