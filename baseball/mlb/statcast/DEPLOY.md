# Deploying the baseball Statcast pipeline to GCP

This deploys a Cloud Run **Job** (not a Service, since this isn't a web
app, it's a script that runs to completion once a day) that pulls
yesterday's Statcast data via pybaseball and appends it to BigQuery,
triggered daily by Cloud Scheduler.

All commands use `gcloud`. You don't need Docker installed locally:
`gcloud builds submit` builds the image in the cloud via Cloud Build.

## 0. Prerequisites

- A GCP project with billing enabled (required even to stay within the
  Always Free tier; set a budget alert, see the note at the bottom).
- `gcloud` CLI installed and authenticated (`gcloud auth login`).

## 1. Set variables and select your project

```bash
export PROJECT_ID=<your-gcp-project-id>
export REGION=us-central1
export REPO=maydaystats
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/baseball-pipeline

gcloud config set project $PROJECT_ID
```

## 2. Enable the required APIs (one-time)

```bash
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  bigquery.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

## 3. Create an Artifact Registry repo for the container image (one-time)

```bash
gcloud artifacts repositories create $REPO \
  --repository-format=docker \
  --location=$REGION
```

## 4. Build and push the image

Run this from inside the `baseball/mlb/statcast/` directory (where the
Dockerfile lives):

```bash
gcloud builds submit --tag $IMAGE .
```

## 5. Create the Cloud Run Job

```bash
gcloud run jobs create baseball-statcast-pipeline \
  --image $IMAGE \
  --region $REGION \
  --set-env-vars BQ_DATASET=mlb_statcast,BQ_TABLE=pitches \
  --max-retries 1 \
  --task-timeout 600
```

## 6. Grant the job's own service account BigQuery access

```bash
export JOB_SA=$(gcloud run jobs describe baseball-statcast-pipeline \
  --region $REGION \
  --format='value(spec.template.spec.template.spec.serviceAccountName)')

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.jobUser"
```

## 7. Test it manually once, before scheduling

```bash
gcloud run jobs execute baseball-statcast-pipeline --region $REGION
```

Watch the logs (Cloud Console > Cloud Run > Jobs > baseball-statcast-pipeline
> Logs), or tail from the CLI:

```bash
gcloud beta run jobs executions logs read --job baseball-statcast-pipeline --region $REGION
```

If the whole recent window had no MLB games (off-days between seasons,
etc.) you'll see "No rows to load; skipping." That's expected, not a
bug. You may also see "Skipping N rows already loaded for dates: [...]"
on a normal run - that's the dedup check working as intended, since
`fetch_recent()` deliberately re-fetches the last few days on every run.

To backfill a specific historical range instead of the rolling window,
override the env vars for a one-off manual run:

```bash
gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2025-04-01,END_DATE=2025-04-07
```

## 7b. Backfilling the full 2026 season

Only July 7-9 was ever actually loaded (see the "First Pitches" post's
history - that job was a pipeline validation, not a season backfill).
2026 Opening Day was March 25, so there are roughly three and a half
months of pitch-level data missing. Statcast is pitch-by-pitch, so this
is a lot more data per day than the boxscore-style pipelines - bump both
the timeout and the memory first (a full month in memory as one
DataFrame OOM'd at Cloud Run's default 512Mi), and run it in two-week
chunks rather than monthly:

```bash
gcloud run jobs update baseball-statcast-pipeline \
  --region $REGION \
  --task-timeout 3600 \
  --memory 2Gi

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-03-25,END_DATE=2026-03-31

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-04-01,END_DATE=2026-04-15

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-04-16,END_DATE=2026-04-30

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-05-01,END_DATE=2026-05-15

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-05-16,END_DATE=2026-05-31

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-06-01,END_DATE=2026-06-15

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-06-16,END_DATE=2026-06-30

gcloud run jobs execute baseball-statcast-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-07-01,END_DATE=2026-07-06
```

If a chunk still OOMs at 2Gi, split it again into one-week pieces
rather than raising memory further - Statcast's row-per-pitch density
varies with games-per-day, and smaller chunks are cheaper to retry than
a bigger memory tier.

Check each execution's logs before moving to the next chunk:

```bash
gcloud beta run jobs executions logs read <execution-id> --region $REGION
```

`bigquery_loader.py`'s dedup means it's safe to re-run any chunk, or run
them out of order, or overlap with the July 7-9 data already loaded -
anything already in the table for a given `game_date` gets skipped
rather than duplicated.

The `--task-timeout 3600` and `--memory 2Gi` changes persist on the job
going forward (unlike `--update-env-vars`, which is a one-time
override) - no need to set either back down afterward, since a larger
allowance only matters if a run actually needs it. The daily scheduled
run (a 3-day rolling window, not a multi-week chunk) fits comfortably
inside both, so this doesn't change normal day-to-day cost.

## 8. Create a dedicated service account for Cloud Scheduler to invoke the job

```bash
gcloud iam service-accounts create scheduler-invoker \
  --display-name "Cloud Scheduler Cloud Run invoker"

gcloud run jobs add-iam-policy-binding baseball-statcast-pipeline \
  --region $REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 9. Schedule the daily run

9am UTC turned out not to be a safe buffer: in production this ran into
Statcast's search export lagging a few hours behind when the previous
day's games actually finished, so the 9am UTC run sometimes fetched a
real game day and got back 0 rows. 3pm UTC (adjust to your timezone)
gives Baseball Savant a full morning to catch up before the pull runs:

```bash
gcloud scheduler jobs create http baseball-statcast-daily \
  --location $REGION \
  --schedule="0 15 * * *" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/baseball-statcast-pipeline:run" \
  --http-method POST \
  --oauth-service-account-email scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com
```

If you already created this scheduler job with the old `0 9 * * *`
schedule, update it in place rather than recreating it:

```bash
gcloud scheduler jobs update http baseball-statcast-daily \
  --location $REGION \
  --schedule="0 15 * * *"
```

The real fix for the underlying issue lives in the pipeline code, not
just the schedule time: `fetch.py`'s `fetch_recent()` pulls a rolling
3-day window (not just a single "yesterday") on every run, and
`bigquery_loader.load_dataframe()` dedups against `game_date` values
already in the table before appending. So even if a run ever does land
before that day's data is published, the next day's run picks it up
automatically instead of that day staying empty forever - see the
docstrings on both for the full reasoning. This also means it's safe
to manually re-run the job at any time without worrying about
duplicating rows.

From here it runs itself. Check BigQuery (`mlb_statcast.pitches`) each
morning to confirm rows are landing.

## Budget safety net (optional but recommended)

Even "Always Free" usage requires a billing account attached. Set a
budget alert so you get emailed if anything unexpected spikes:

Console > Billing > Budgets & alerts > Create budget > set to $1.
This costs nothing itself; it's just a tripwire.

## Why Cloud Run Jobs instead of a Cloud Function

A Cloud Run Job runs to completion and then stops. No idle server, no
HTTP endpoint to secure, no cold-start web framework needed. It's the
right primitive for "run this script once a day and exit," which is
exactly this workload. Cloud Functions/Cloud Run Services are a better
fit for something that needs to respond to live HTTP requests, which
this doesn't.
