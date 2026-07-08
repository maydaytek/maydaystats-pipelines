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
  See `hockey/DEPLOY.md`. Note: the field-name mapping in `fetch.py` is a
  best-effort guess at the undocumented NHL API schema and needs to be
  checked against the first real run's logs.
- `volleyball-ncaa-men/`: NCAA men's volleyball boxscore data, pulled from
  NCAA's own backend through a self-hosted, open-source proxy that
  translates its GraphQL API into simple REST calls. See
  `volleyball-ncaa-men/DEPLOY.md` for the full explanation and the proxy's
  own deploy step. Spring season (January-May).
- `volleyball-ncaa-women/`: same NCAA data source, proxy, and pattern as
  the men's pipeline, sharing most of its code and the same proxy
  deployment. Fall season (August-December). See
  `volleyball-ncaa-women/DEPLOY.md`.
- `volleyball-mlv/` (planned): pro volleyball data for Major League
  Volleyball, the league Indy Ignite plays in. Data source still to be
  finalized - see the pipelines' history for the alternatives considered.

## Why this architecture

Everything runs in GCP's Always Free tier (Cloud Run Jobs, Cloud Scheduler,
BigQuery storage/queries at this data volume) rather than on local
hardware, so the pipeline doesn't depend on a home connection or a home
server staying powered on. It just runs, every day, on its own.
