# Deploying the NCAA men's volleyball pipeline to GCP

Same shape as the baseball and hockey pipelines: a Cloud Run Job pulls
completed games and appends their boxscores to BigQuery, triggered daily
by Cloud Scheduler. Run these from inside the `volleyball-ncaa-men/` directory.

**Data source note:** the underlying data comes from NCAA's own backend
(`sdataprod.ncaa.com` / `data.ncaa.com`), the same servers ncaa.com's own
website calls - first-party NCAA data, same tier as MLB's Statcast or the
NHL's API in terms of provenance. What's undocumented is the specific
access technique: ncaa.com's frontend uses GraphQL "persisted queries"
(a query hash instead of query text), which the open-source
[NCAA API](https://github.com/henrygd/ncaa-api) project has already
reverse-engineered and translates into simple REST paths. Rather than
depend on that project's public demo server (shared rate limit, no
uptime guarantee, and it's someone else's box), step 2 below self-hosts
the same open-source code as our own private Cloud Run Service. We get
first-party data without maintaining the GraphQL reverse-engineering
ourselves, and without depending on a stranger's server staying up.

**Season timing:** men's volleyball is a spring sport, roughly January
through the national championship in early May. A daily "yesterday" run
from June through December will correctly return zero rows every time;
that's expected, not a bug.

## 1. Set variables (reusing the same project/region as baseball and hockey)

```bash
export PROJECT_ID=maydaystats
export REGION=us-central1
export REPO=maydaystats
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/volleyball-ncaa-men-pipeline
```

APIs, the Artifact Registry repo, and billing are already set up from the
baseball pipeline - no need to repeat those steps.

## 2. Deploy the self-hosted NCAA API proxy (one-time, shared with women's pipeline)

This deploys the open-source `henrygd/ncaa-api` project directly from its
public Docker Hub image, as a private Cloud Run Service. Both the men's
and women's volleyball pipelines call this same instance, so this step
only needs to happen once.

```bash
gcloud run deploy ncaa-api-proxy \
  --image=docker.io/henrygd/ncaa-api \
  --region=$REGION \
  --port=3000 \
  --no-allow-unauthenticated \
  --min-instances=0 \
  --max-instances=1 \
  --memory=512Mi

export NCAA_API_URL=$(gcloud run services describe ncaa-api-proxy \
  --region=$REGION \
  --format='value(status.url)')

echo $NCAA_API_URL
```

`--no-allow-unauthenticated` keeps this private: only callers with
`roles/run.invoker` on this service (granted in step 5) can reach it.
Anyone else hitting the URL gets a 403.

## 3. Build and push the volleyball-ncaa-men pipeline image

```bash
gcloud builds submit --tag $IMAGE .
```

## 4. Create the Cloud Run Job

```bash
gcloud run jobs create volleyball-ncaa-men-boxscore-pipeline \
  --image $IMAGE \
  --region $REGION \
  --set-env-vars BQ_DATASET=ncaa_volleyball_men,BQ_TABLE=boxscores,NCAA_API_BASE_URL=$NCAA_API_URL \
  --max-retries 1 \
  --task-timeout 3600
```

The longer task timeout (1 hour, vs. the 10-minute default) gives enough
room for a multi-month backfill. Daily runs finish in seconds.

## 5. Grant IAM access: BigQuery and the NCAA API proxy

```bash
export JOB_SA=$(gcloud run jobs describe volleyball-ncaa-men-boxscore-pipeline \
  --region $REGION \
  --format='value(spec.template.spec.template.spec.serviceAccountName)')

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/bigquery.jobUser"

gcloud run services add-iam-policy-binding ncaa-api-proxy \
  --region $REGION \
  --member="serviceAccount:$JOB_SA" \
  --role="roles/run.invoker"
```

(If this job shares the same default compute service account as the other
pipelines, the BigQuery bindings will already be in place and those two
commands are no-ops - that's fine, they're idempotent. The `run.invoker`
binding on `ncaa-api-proxy` is the one binding this pipeline actually
needs freshly granted.)

## 6. Backfill the completed 2025-26 season

The 2025-26 men's season already finished (national championship was in
early May 2026), so there's a full season available right away instead of
waiting for the next one to start. Backfill it in a couple of chunks
rather than one giant call, so a failure partway through only costs one
chunk:

```bash
gcloud run jobs execute volleyball-ncaa-men-boxscore-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-01-01,END_DATE=2026-03-15

gcloud run jobs execute volleyball-ncaa-men-boxscore-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2026-03-16,END_DATE=2026-05-05
```

Check each execution's logs before moving to the next:

```bash
gcloud beta run jobs executions logs read <execution-id> --region $REGION
```

Look for "Fetched N rows" with N > 0 on ranges that include real game
dates. If a whole range comes back empty, double check the date range
actually falls within the season before assuming something's broken -
most of the calendar year is a correct zero for this sport. If a run
fails outright with an auth error, double check the `run.invoker` binding
from step 5 landed on the right service account.

## 7. Reuse the existing scheduler-invoker service account

```bash
gcloud run jobs add-iam-policy-binding volleyball-ncaa-men-boxscore-pipeline \
  --region $REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 8. Schedule the daily run

```bash
gcloud scheduler jobs create http volleyball-ncaa-men-boxscore-daily \
  --location $REGION \
  --schedule="30 9 * * *" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/volleyball-ncaa-men-boxscore-pipeline:run" \
  --http-method POST \
  --oauth-service-account-email scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com
```

Offset a few minutes from the other pipelines' schedules just so they
don't all kick off at the same instant - not required, just tidy.

From here it runs itself: zero rows most of the year, real rows every day
once the next men's season starts in January.
