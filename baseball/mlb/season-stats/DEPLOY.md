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

**Each row is a snapshot, and the table is a history of them.** A
player's season stats change every day as they play more games, so each
row is stamped with a `snapshot_date` and every daily run appends a new
set of snapshots rather than overwriting the table - the table builds up
a day-by-day history of where the leaderboards stood, which is what lets
later queries trend something like "how did the batting average leaders
move heading into the All-Star Game." Re-running the job on the same day
is still safe: `bigquery_loader.append_snapshot()` deletes any existing
rows for that day's `snapshot_date` before appending, so a manual re-run
replaces that day's snapshot instead of duplicating it. Queries that only
want "the season as it stands right now" should filter to the latest
`snapshot_date` in a table (or use the `*_latest` views - see step 8).

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
team_batting, team_pitching) and four "Appended N rows into ..." lines.
Then check BigQuery: `mlb_season_stats.batting` and
`mlb_season_stats.pitching` should each have several hundred rows (every
player with at least one plate appearance or batter faced this season,
not just qualified regulars - `playerPool=ALL` in `fetch.py`), and
`team_batting`/`team_pitching` should have 30 rows each, one per team.

### Updating the code on an already-deployed job

If the job already exists (e.g. you already ran steps 1-5 once and are
now picking up a code change), rebuilding and pushing to the same image
tag does **not** automatically update the job - Cloud Run Jobs resolve
the image to a specific digest at deploy time. Force it to pick up the
new build:

```bash
gcloud builds submit --tag $IMAGE .

gcloud run jobs update baseball-season-stats-pipeline \
  --region $REGION \
  --image $IMAGE

gcloud run jobs execute baseball-season-stats-pipeline --region $REGION
```

The very first run under the new append-based code will delete-and-replace
today's snapshot (from the earlier truncate-based version) rather than
create a duplicate for today - safe either way.

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

## 8. Create the *_latest convenience views (optional but recommended)

Since these tables now accumulate a snapshot per day, "who's leading the
league right now" queries need to filter to the most recent
`snapshot_date`. These views do that filtering once so downstream
queries (like a post's BigQuery cell) don't have to repeat it:

```bash
for table in batting pitching team_batting team_pitching; do
  bq query --use_legacy_sql=false "
    CREATE OR REPLACE VIEW \`$PROJECT_ID.mlb_season_stats.${table}_latest\` AS
    SELECT * FROM \`$PROJECT_ID.mlb_season_stats.${table}\`
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM \`$PROJECT_ID.mlb_season_stats.${table}\`)
  "
done
```

Trend queries (e.g. "how did this player's home run total change over
the season") should query the base tables directly instead, filtering
`snapshot_date` to a range rather than a single day.

## Why a separate pipeline instead of extending `baseball/mlb/statcast/`

Different shape of data (season snapshot history vs. pitch-level event
log, both append-based but keyed differently - `snapshot_date` here vs.
`game_date` there), different source (MLB Stats API vs. Baseball
Savant), and different failure modes - keeping them as separate Cloud
Run Jobs means an issue with one can't affect the other's daily run.
