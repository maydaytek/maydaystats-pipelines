# Deploying the MLB season-stats pipeline to GCP

This deploys a second, separate Cloud Run Job alongside the existing
`baseball/mlb/statcast/` pipeline. Where Statcast is pitch-level (one row
per pitch, no player names for batters), this pulls MLB's own Stats API
(`statsapi.mlb.com`) - real batting, pitching, and team stats with names
already attached, and the right source for "who's leading the league in
X" style stat rankings.

**Why MLB's own API instead of FanGraphs:** the first version of this
pipeline used pybaseball's FanGraphs wrapper, which started returning
403s from a Cloudflare bot wall FanGraphs added. That's a known,
unfixable-client-side issue - see
[pybaseball#479](https://github.com/jldbc/pybaseball/issues/479), where
several other users hit the identical error with no working fix.
`statsapi.mlb.com` is a real first-party JSON API (the same one
MLB.com's own stats pages call), so there's no bot wall to run into. It
was tested directly and returns clean data: real player names, teams,
positions, and all standard/advanced stat fields.

**This is a snapshot, not an event log.** A player's season stats change
every day as they play more games, so this job doesn't append - it
replaces all four tables with the latest full-season numbers on every
run (`WRITE_TRUNCATE`, see `bigquery_loader.py`). Each row also gets a
`snapshot_date` column so you can tell how fresh the data is, but there's
no history of past days kept, unlike the Statcast pipeline.

Reuses the same GCP project, Artifact Registry repo, and
`scheduler-invoker` service account already set up for
`baseball/mlb/statcast/` - skip straight to step 1 if you've already
deployed that pipeline.

## 1. Set variables

```bash
export PROJECT_ID=maydaystats
export REGION=us-central1
export REPO=maydaystats
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/baseball-season-stats-pipeline
```

## 2. Build and push the image

Run this from inside the `baseball/mlb/season-stats/` directory:

```bash
gcloud builds submit --tag $IMAGE .
```

## 3. Create the Cloud Run Job

```bash
gcloud run jobs create baseball-season-stats-pipeline \
  --image $IMAGE \
  --region $REGION \
  --set-env-vars BQ_DATASET=mlb_season_stats \
  --max-retries 1 \
  --task-timeout 600
```

`SEASON` defaults to the current calendar year if not set; override it
with `--update-env-vars SEASON=2025` for a one-off historical pull.
Unlike the Statcast pipeline, this fetches a small amount of data
(hundreds of rows, not hundreds of thousands), so the default memory
allocation should be plenty - no `--memory` override expected here.

## 4. Grant the job's service account BigQuery access

```bash
export JOB_SA=$(gcloud run jobs describe baseball-season-stats-pipeline \
  --region $REGION \
  --format='value(spec.template.spec.template.spec.serviceAccountName)')

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.jobUser"
```

(Idempotent no-ops if this job shares the default compute service account
with the other pipelines.)

## 5. Test it manually once

```bash
gcloud run jobs execute baseball-season-stats-pipeline --region $REGION
```

```bash
gcloud beta run jobs executions logs read <execution-id> --region $REGION
```

You should see four "fetched N rows" lines (batting, pitching,
team_batting, team_pitching) and four "Loaded N rows into ..." lines.
Then check BigQuery: `mlb_season_stats.batting` and
`mlb_season_stats.pitching` should each have several hundred rows (every
player with at least one plate appearance or batter faced this season,
not just qualified regulars - `playerPool=ALL` in `fetch.py`), and
`team_batting`/`team_pitching` should have 30 rows each, one per team.

If you deployed the earlier FanGraphs-based version of this pipeline
first, it left behind an empty `mlb_fangraphs` dataset (the 403s failed
before any table got created) - safe to delete once this one's
confirmed working:

```bash
bq rm -r -d $PROJECT_ID:mlb_fangraphs
```

## 6. Reuse the existing scheduler-invoker service account

```bash
gcloud run jobs add-iam-policy-binding baseball-season-stats-pipeline \
  --region $REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 7. Schedule the daily run

Offset from the Statcast pipeline's 3pm UTC run so they don't compete for
the same window:

```bash
gcloud scheduler jobs create http baseball-season-stats-daily \
  --location $REGION \
  --schedule="20 15 * * *" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/baseball-season-stats-pipeline:run" \
  --http-method POST \
  --oauth-service-account-email scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com
```

From here it refreshes itself daily. The existing pipeline-failure
monitoring alert (see `../../../MONITORING.md`) already covers this job
too - it isn't scoped to specific job names, so a failure here triggers
the same email alert as any other pipeline.

## Why a separate pipeline instead of extending `baseball/mlb/statcast/`

Different shape of data (season snapshot vs. pitch-level event log,
`WRITE_TRUNCATE` vs. append-and-dedup), different source (MLB Stats API
vs. Baseball Savant), and different failure modes - keeping them as
separate Cloud Run Jobs means an issue with one can't affect the other's
daily run.
