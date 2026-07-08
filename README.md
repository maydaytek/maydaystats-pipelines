# maydaystats data pipelines

Scheduled ETL jobs that feed the maydaystats.com analytics content.
Each sport gets its own subfolder with its own fetch logic, Dockerfile,
and deploy guide, but they all follow the same shape:

1. Pull data from a public source (Statcast, NHL API, etc.)
2. Load it into BigQuery, appending to a growing historical table
3. Run daily via Cloud Run Job + Cloud Scheduler — no server to babysit

## Folders

- `baseball/` — MLB Statcast pitch-level data via `pybaseball`. Start here;
  see `baseball/DEPLOY.md` for the full GCP setup.
- `hockey/` — NHL play-by-play/boxscore data (planned next, same pattern).

## Why this architecture

Everything runs in GCP's Always Free tier (Cloud Run Jobs, Cloud Scheduler,
BigQuery storage/queries at this data volume) rather than on local
hardware, so the pipeline doesn't depend on a home connection or a home
server staying powered on — it just runs, every day, on its own.
