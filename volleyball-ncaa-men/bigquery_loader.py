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


def load_dataframe(
    client: bigquery.Client,
    df: pd.DataFrame,
    dataset_id: str,
    table_id: str,
) -> None:
    """Append a DataFrame of boxscore rows into the target table."""
    if df.empty:
        print("No rows to load; skipping.")
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
