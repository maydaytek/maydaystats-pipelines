# Deploying the hockey boxscore pipeline to GCP

Same shape as the baseball pipeline: a Cloud Run Job pulls yesterday's
NHL boxscores and appends them to BigQuery, triggered daily by Cloud
Scheduler. Run these from inside the `hockey/` directory.

**Field-name mapping:** the flatten functions in `fetch.py` started as a
best-effort guess at the NHL API's undocumented boxscore schema. That
mapping has since been validated end-to-end: a full 2025-26 season
(regular season and playoffs) is backfilled in BigQuery, and the
site's hockey year-in-review post cross-checks its numbers against
those same columns. If a future season introduces new fields or
renames existing ones, the same symptom applies - key stat columns
coming back null in a run's logs is the signal to check `fetch.py`'s
mapping against the live API response.

## 1. Set variables (reusing the same project/region as baseball)

```bash
export PROJECT_ID=maydaystats
export REGION=us-central1
export REPO=maydaystats
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/hockey-pipeline
```

APIs, the Artifact Registry repo, and billing are already set up from the
baseball pipeline - no need to repeat those steps.

## 2. Build and push the image

```bash
gcloud builds submit --tag $IMAGE .
```

## 3. Create the Cloud Run Job

```bash
gcloud run jobs create hockey-boxscore-pipeline \
  --image $IMAGE \
  --region $REGION \
  --set-env-vars BQ_DATASET=nhl_stats,BQ_TABLE=boxscores \
  --max-retries 1 \
  --task-timeout 600
```

## 4. Grant the job's service account BigQuery access

```bash
export JOB_SA=$(gcloud run jobs describe hockey-boxscore-pipeline \
  --region $REGION \
  --format='value(spec.template.spec.template.spec.serviceAccountName)')

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.jobUser"
```

(If this job ends up sharing the same default compute service account as
the baseball job, these bindings will already be in place and these two
commands are no-ops - that's fine, they're idempotent.)

## 5. Test it manually - this is the real check on the field-name guesses

```bash
gcloud run jobs execute hockey-boxscore-pipeline --region $REGION
```

```bash
gcloud beta run jobs executions logs read <execution-id> --region $REGION
```

Look for "Fetched N rows" with N > 0 on a day the NHL actually played
games. Then check the BigQuery table itself - if `goals`, `assists`,
`points`, etc. are populated with real numbers rather than all-null,
the schema mapping is correct. If they're null, open a live boxscore
response (`curl https://api-web.nhle.com/v1/gamecenter/<game-id>/boxscore`)
and compare its actual field names against `fetch.py`'s `_flatten_skaters`/
`_flatten_goalies` functions.

To backfill a historical range instead of "yesterday":

```bash
gcloud run jobs execute hockey-boxscore-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2025-10-08,END_DATE=2025-10-14
```

## 6. Reuse the existing scheduler-invoker service account

No need for a second service account - grant the same one from the
baseball setup permission to invoke this job too:

```bash
gcloud run jobs add-iam-policy-binding hockey-boxscore-pipeline \
  --region $REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 7. Schedule the daily run

```bash
gcloud scheduler jobs create http hockey-boxscore-daily \
  --location $REGION \
  --schedule="15 9 * * *" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/hockey-boxscore-pipeline:run" \
  --http-method POST \
  --oauth-service-account-email scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com
```

Offset a few minutes from the other pipelines' schedules just so they
don't all kick off at the same instant - not required, just tidy.
(Baseball's own schedule later moved to `0 15 * * *` after its early
9am UTC run turned out to be too early for its data source - see
baseball's DEPLOY.md. This job hasn't shown that symptom, but see the
note below on why it's protected either way.)

Like baseball, this pipeline fetches a rolling window rather than a
single "yesterday" (`fetch.py`'s `fetch_recent()`, 3 days by default),
and `bigquery_loader.load_dataframe()` dedups against `game_date`
already in BigQuery before appending. So even if this job's source
data is ever slow to publish on a given morning, the next day's run
picks up whatever was missed automatically instead of that day staying
empty. It's also what makes off-season runs (NHL's summer break, June
through September) and manual re-runs both perfectly safe - the window
just correctly returns zero rows, or dedup skips anything already
loaded, rather than anything getting double-loaded.

From here it runs itself, same as baseball.
