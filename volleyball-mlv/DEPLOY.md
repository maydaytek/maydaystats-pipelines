# Deploying the MLV (Major League Volleyball) pipeline to GCP

Same overall shape as the baseball, hockey, and NCAA volleyball pipelines:
a Cloud Run Job appends rows to BigQuery, triggered by Cloud Scheduler.
Run these from inside the `volleyball-mlv/` directory.

**Data source note:** provolleyball.com's own JSON API (built by WMT
Digital on top of VolleyStation's stats software) is first-party and
needs no auth, no proxy, and no API key - same tier of provenance as MLB's
Statcast or the NHL API. Unlike those two, it never exposes a per-player,
per-match boxscore as JSON; that only exists as a PDF. Every completed
`schedule-event`'s `volleyStationMatch` include carries a `report` field
pointing at a public PDF (VolleyStation's own "Match Box Score" export,
same fixed layout for every match) hosted on DigitalOcean Spaces. This
pipeline downloads that PDF per match and parses it - see `parser.py` for
the layout quirks it works around, and the module docstring there for why
each workaround exists.

**No self-hosted proxy needed here**, unlike the NCAA pipelines - this is
genuinely the league's own first-party API, not someone else's
reverse-engineered demo server.

**Two PDF layouts:** MLV/VolleyStation switched box score PDF templates
partway through the 2025-26 season. Roughly the first two-thirds of the
regular season used an older "Match report" layout with different columns
and no assist/dig tracking at all; the rest use the newer "Match Box
Score" layout everything else in this project was originally built
against. `fetch.py` tries the newer layout first and falls back to the
older one (`parse_box_score_legacy` in `parser.py`) automatically - every
row in `boxscores` carries a `source_format` ('new' or 'legacy') so this
is visible per row rather than silently averaged away. `assists`,
`setting_errors`, `good_passes`, and `digs` are always null on `legacy`
rows; that's a real gap in what the older layout tracked, not a parsing
bug. The legacy layout also contributes a few extra columns with no
equivalent in the newer one (`points_break_points`,
`points_net_serve_attack`, `reception_attempts`, `reception_positive_pct`,
`reception_excellent_pct`, `attack_blocked_by_opponent`) - these are null
on every `new`-format row.

**Data quality flag:** every match row in the `matches` table also carries
a `checksum_ok` boolean, independent of which layout it used. Each parser
cross-checks its own work by summing every parsed player's stats and
comparing against the PDF's own printed team-total row ("Team Total" in
the new layout, "Players total" in the legacy one); `checksum_ok = false`
means that comparison failed for at least one team in that match, so the
boxscore rows for it should be treated with more suspicion than the rest
(filter `WHERE checksum_ok` for anything sensitive). In testing against
three real matches spanning both layouts (a new-layout regular-season
game, the new-layout May 2026 championship semifinal, and a legacy-layout
March game), every column matched exactly on all three, so mismatches
should be rare - but if a PDF layout doesn't match EITHER parser at all,
`fetch.py` skips that match entirely (logged as a WARNING) rather than
loading anything for it; if that starts showing up regularly, there may
be a third layout somewhere in the season worth investigating.

**No "yesterday" date window:** MLV plays 2-4 games a week, not most days,
so this pipeline doesn't fetch "yesterday's games" the way the NCAA/
baseball/hockey pipelines do. Instead it fetches every completed match
across every season the API knows about and skips whatever's already in
BigQuery (by `volley_station_match_id`, not `schedule_event_id` - a
postponed-then-rescheduled game can leave two schedule_event rows pointing
at the same underlying match, and the dedup needs to survive that). That
makes a full backfill and a normal scheduled run the exact same code path.

## 1. Set variables (reusing the same project/region as the other pipelines)

```bash
export PROJECT_ID=maydaystats
export REGION=us-central1
export REPO=maydaystats
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/volleyball-mlv-pipeline
```

APIs, the Artifact Registry repo, and billing are already set up from the
baseball pipeline - no need to repeat those steps.

## 2. Build and push the pipeline image

```bash
gcloud builds submit --tag $IMAGE .
```

## 3. Create the Cloud Run Job

```bash
gcloud run jobs create volleyball-mlv-boxscore-pipeline \
  --image $IMAGE \
  --region $REGION \
  --set-env-vars BQ_DATASET=mlv_volleyball,BQ_MATCHES_TABLE=matches,BQ_BOXSCORES_TABLE=boxscores \
  --max-retries 1 \
  --task-timeout 3600
```

The longer task timeout (1 hour, vs. the 10-minute default) leaves room
for the first run, which backfills the entire 2025-26 season (117
schedule-events, each requiring a PDF download) in one go.

## 4. Grant IAM access: BigQuery

```bash
export JOB_SA=$(gcloud run jobs describe volleyball-mlv-boxscore-pipeline \
  --region $REGION \
  --format='value(spec.template.spec.template.spec.serviceAccountName)')

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.jobUser"
```

(If this job shares the same default compute service account as the other
pipelines, these bindings will already be in place and this step is a
no-op - that's fine, it's idempotent.)

## 5. Backfill the completed 2025-26 season

The 2025-26 season already finished (championship semifinals were played
May 7-9, 2026), so the very first execution loads the whole thing - there
is no separate "backfill mode," the default behavior already fetches
everything not yet in BigQuery:

```bash
gcloud run jobs execute volleyball-mlv-boxscore-pipeline --region $REGION
```

Check the execution's logs:

```bash
gcloud beta run jobs executions logs read <execution-id> --region $REGION
```

Look for a line like "Fetched 117 new matches, ~3400 new player boxscore
rows" (117 schedule-events in the 2025-26 season, most with ~28-31 rostered
players per match). Watch for `WARNING: schedule_event ... failed the
team-total checksum` lines - a few of those aren't fatal (that match still
loads, just flagged via `checksum_ok = false`), but if most matches are
logging that warning, something about the live PDF layout differs from
what `parser.py` was built against and is worth a closer look before
trusting the data.

## 6. Reuse the existing scheduler-invoker service account

```bash
gcloud run jobs add-iam-policy-binding volleyball-mlv-boxscore-pipeline \
  --region $REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 7. Schedule the daily run

```bash
gcloud scheduler jobs create http volleyball-mlv-boxscore-daily \
  --location $REGION \
  --schedule="45 9 * * *" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/volleyball-mlv-boxscore-pipeline:run" \
  --http-method POST \
  --oauth-service-account-email scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com
```

Offset a few minutes from the other pipelines' schedules just so they
don't all kick off at the same instant - not required, just tidy.

Most days this will fetch zero new matches (there's no active MLV season
right now - the 2026-27 season hasn't started as of this writing, and the
API already reflects that with zero scheduled events under its season_id).
That's expected, not a bug: it'll start returning real rows automatically
once the next season's schedule goes live, without needing a code change
or a new deploy, since the pipeline always asks the API for every season
it knows about rather than a hardcoded season_id.

## 8. Verify

```sql
SELECT COUNT(*) FROM `maydaystats.mlv_volleyball.matches`;
SELECT COUNT(*) FROM `maydaystats.mlv_volleyball.boxscores`;
SELECT player_name, team, SUM(kills) AS kills
FROM `maydaystats.mlv_volleyball.boxscores`
GROUP BY player_name, team
ORDER BY kills DESC
LIMIT 10;
```

Sanity-check a specific match against its own PDF if anything looks off -
e.g. schedule_event 637 (the Indy/Omaha championship semifinal) should sum
to a 2-3 Indy loss with Lydia Martyn leading Indy in points (19) and Sarah
Parsons leading Omaha (20), per the source PDF at
`https://fra1.digitaloceanspaces.com/pls-api/matches/2464510-824334818cf8de7aaad9bd8770c90228/2026-05-07-IND-OMA.pdf`.
