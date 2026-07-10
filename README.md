# maydaystats data pipelines

Scheduled ETL jobs that feed the maydaystats.com analytics content.
Each sport gets its own subfolder with its own fetch logic, Dockerfile,
and deploy guide, but they all follow the same shape:

1. Pull data from a public source (Statcast, NHL API, etc.)
2. Load it into BigQuery, appending to a growing historical table
3. Run daily via Cloud Run Job + Cloud Scheduler, no server to babysit

## Folders

- `baseball/`: MLB Statcast pitch-level data via `pybaseball`. Start here;
  see `baseball/DEPLOY.md` for the full GCP setup.
- `hockey/`: NHL boxscore data via the NHL API, same pattern as baseball.
  See `hockey/DEPLOY.md`. The field-name mapping in `fetch.py` was
  originally a best-effort guess at the undocumented NHL API schema;
  it's since been validated against a full backfilled season (regular
  season and playoffs) and a fact-checked year-in-review post built on
  top of it.
- `volleyball-ncaa-men/`: NCAA men's volleyball boxscore data, pulled from
  NCAA's own backend through a self-hosted, open-source proxy that
  translates its GraphQL API into simple REST calls. See
  `volleyball-ncaa-men/DEPLOY.md` for the full explanation and the proxy's
  own deploy step. Spring season (January-May).
- `volleyball-ncaa-women/`: same NCAA data source, proxy, and pattern as
  the men's pipeline, sharing most of its code and the same proxy
  deployment. Fall season (August-December). See
  `volleyball-ncaa-women/DEPLOY.md`.
- `volleyball-mlv/`: pro volleyball data for Major League Volleyball, the
  league Indy Ignite plays in, scraped from per-match PDF scoresheets
  since the league's own API never exposes boxscores as JSON. See
  `volleyball-mlv/DEPLOY.md` for the full explanation and the parser's
  data-quality notes. A full 2025-26 season backfill (99 matches) is
  live in BigQuery, with a daily Cloud Scheduler job picking up new
  matches once the next season starts.

## Why this architecture

Everything runs in GCP's Always Free tier (Cloud Run Jobs, Cloud Scheduler,
BigQuery storage/queries at this data volume) rather than on local
hardware, so the pipeline doesn't depend on a home connection or a home
server staying powered on. It just runs, every day, on its own.

## Monitoring

See `MONITORING.md` for the Cloud Monitoring alert policy that emails
on any job execution failure across all five pipelines. Every
pipeline's daily fetch also self-heals against a source publishing
data late: `fetch_recent()` pulls a rolling multi-day window instead of
a single day, and `bigquery_loader.py` dedups against what's already in
BigQuery before appending - see any pipeline's `DEPLOY.md` for the
reasoning behind that pattern.
