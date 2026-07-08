# Deploying the NCAA women's volleyball pipeline to GCP

Same shape as the other pipelines: a Cloud Run Job pulls completed games
and appends their boxscores to BigQuery, triggered daily by Cloud
Scheduler. Run these from inside the `volleyball-ncaa-women/` directory.

**Data source note:** this uses the same NCAA data as the men's pipeline,
by way of the same self-hosted `ncaa-api-proxy` Cloud Run Service. See
`volleyball-ncaa-men/DEPLOY.md` for the full explanation of why it's
self-hosted and what data source it actually reaches. If you've already
deployed `ncaa-api-proxy` while setting up the men's pipeline, skip
straight to step 2 below - it's shared between both.

**Season timing:** women's volleyball is a fall sport, roughly late
August through the national championship in mid-December. A daily
"yesterday" run from January through August will correctly return zero
rows every time; that's expected, not a bug.

## 1. Set variables (reusing the same project/region as the other pipelines)

```bash
export PROJECT_ID=maydaystats
export REGION=us-central1
export REPO=maydaystats
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/volleyball-ncaa-women-pipeline

export NCAA_API_URL=$(gcloud run services describe ncaa-api-proxy \
  --region=$REGION \
  --format='value(status.url)')
```

If `NCAA_API_URL` comes back empty, `ncaa-api-proxy` hasn't been deployed
yet - go run step 2 of `volleyball-ncaa-men/DEPLOY.md` first, then come back.

## 2. Build and push the image

```bash
gcloud builds submit --tag $IMAGE .
```

## 3. Create the Cloud Run Job

```bash
gcloud run jobs create volleyball-ncaa-women-boxscore-pipeline \
  --image $IMAGE \
  --region $REGION \
  --set-env-vars BQ_DATASET=ncaa_volleyball_women,BQ_TABLE=boxscores,NCAA_API_BASE_URL=$NCAA_API_URL \
  --max-retries 1 \
  --task-timeout 3600
```

## 4. Grant IAM access: BigQuery and the NCAA API proxy

```bash
export JOB_SA=$(gcloud run jobs describe volleyball-ncaa-women-boxscore-pipeline \
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

(Idempotent no-ops for the BigQuery bindings if this job shares the
default compute service account with the other pipelines. If it's the
same service account the men's job already used, the `run.invoker`
binding on `ncaa-api-proxy` will be a no-op too.)

## 5. Backfill the completed 2025 season

The 2025 women's season already finished (national championship was in
mid-December 2025), so there's a full season available right away.
Backfill it in a couple of chunks:

```bash
gcloud run jobs execute volleyball-ncaa-women-boxscore-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2025-08-20,END_DATE=2025-10-31

gcloud run jobs execute volleyball-ncaa-women-boxscore-pipeline \
  --region $REGION \
  --update-env-vars START_DATE=2025-11-01,END_DATE=2025-12-20
```

Check each execution's logs before moving to the next, the same way as
the other pipelines:

```bash
gcloud beta run jobs executions logs read <execution-id> --region $REGION
```

## 6. Reuse the existing scheduler-invoker service account

```bash
gcloud run jobs add-iam-policy-binding volleyball-ncaa-women-boxscore-pipeline \
  --region $REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

## 7. Schedule the daily run

```bash
gcloud scheduler jobs create http volleyball-ncaa-women-boxscore-daily \
  --location $REGION \
  --schedule="45 9 * * *" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/volleyball-ncaa-women-boxscore-pipeline:run" \
  --http-method POST \
  --oauth-service-account-email scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com
```

From here it runs itself: zero rows for most of the year, real rows every
day once the next women's season starts in late August.
