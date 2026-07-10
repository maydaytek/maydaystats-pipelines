# Alerting on pipeline job failures

One Cloud Monitoring alert policy, covering every pipeline's Cloud Run
Job (baseball statcast, baseball season-stats, hockey, both NCAA
volleyball jobs, and MLV) at once: if
any execution fails - a crash, an unhandled exception, a schema error
like the `arm_angle` truncation this session hit - you get an email
within a few minutes, instead of finding out only by manually checking
BigQuery.

**What this does and doesn't catch:** this alerts on a job actually
*failing* (non-zero exit). It does **not** catch a job that runs and
exits cleanly but quietly did nothing useful - that was the original
baseball bug (a real day with real games came back with 0 rows). That
class of problem is handled separately, at the code level: every
pipeline's `fetch_recent()` now pulls a rolling multi-day window and
`bigquery_loader.py` dedups against what's already loaded, so a day
missed for any reason gets picked up automatically on the next run
rather than staying empty. This alert policy is the second layer, for
when something fails outright instead of quietly doing nothing.

## 1. Create an email notification channel (one-time)

Set this to whatever address you want alerts sent to - kept as a local
env var rather than hardcoded here since this repo is public:

```bash
export ALERT_EMAIL=<your-email-address>

gcloud beta monitoring channels create \
  --display-name="Pipeline failure alerts" \
  --type=email \
  --channel-labels=email_address=$ALERT_EMAIL
```

Grab its resource name:

```bash
export CHANNEL_ID=$(gcloud beta monitoring channels list \
  --filter='displayName="Pipeline failure alerts"' \
  --format='value(name)')

echo $CHANNEL_ID
```

## 2. Create the alert policy

This generates the policy file locally (not committed - it embeds your
account-specific channel ID) and applies it:

```bash
cat > /tmp/pipeline-failure-policy.json <<EOF
{
  "displayName": "maydaystats pipeline job execution failed",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "Cloud Run Job task attempt failed",
      "conditionThreshold": {
        "filter": "resource.type=\"cloud_run_job\" AND metric.type=\"run.googleapis.com/job/completed_task_attempt_count\" AND metric.labels.result=\"failed\"",
        "comparison": "COMPARISON_GT",
        "thresholdValue": 0,
        "duration": "0s",
        "aggregations": [
          {
            "alignmentPeriod": "300s",
            "perSeriesAligner": "ALIGN_COUNT",
            "crossSeriesReducer": "REDUCE_NONE",
            "groupByFields": ["resource.label.job_name"]
          }
        ],
        "trigger": { "count": 1 }
      }
    }
  ],
  "notificationChannels": ["$CHANNEL_ID"],
  "alertStrategy": { "autoClose": "604800s" },
  "documentation": {
    "content": "A Cloud Run Job execution failed for one of the maydaystats pipelines. The alert payload names which job (resource.label.job_name). Check why: gcloud run jobs executions list --job=<job-name> --region=us-central1 to find the execution, then gcloud beta run jobs executions logs read <execution-id> --region=us-central1 to read its logs.",
    "mimeType": "text/markdown"
  }
}
EOF

gcloud beta monitoring policies create --policy-from-file=/tmp/pipeline-failure-policy.json
```

One policy, all six jobs - the filter isn't scoped to a specific job
name, so it matches a failure on any of them, and `groupByFields` on
`job_name` means the alert email tells you which pipeline broke rather
than just "something failed somewhere."

## 3. Verify it's live

```bash
gcloud beta monitoring policies list --filter='displayName="maydaystats pipeline job execution failed"'
```

Should show the policy as `ENABLED`. No need to test-fire it manually -
the July 10 truncation error would have triggered exactly this alert
if it had existed at the time.

## Cost

Free. Cloud Monitoring's alerting policies and email notification
channels don't have a paid tier for volume this low - this project's
entire monitoring footprint (six jobs, checked every few minutes)
stays well inside Cloud Monitoring's free allowance.
