"""Fetch NCAA volleyball boxscore data via the NCAA API
(https://github.com/henrygd/ncaa-api), an open-source wrapper that
translates simple REST paths into the exact GraphQL queries ncaa.com's own
website uses against NCAA's own backend (sdataprod.ncaa.com /
data.ncaa.com). The data itself is first-party NCAA data, same as any
other undocumented-but-official league API this project uses.

By default this points at the public demo instance
(https://ncaa-api.henrygd.me), which is rate-limited to 5 requests/second
and has no uptime guarantee. In production, set NCAA_API_BASE_URL to a
self-hosted instance of the same open-source project running as a private
Cloud Run Service in this GCP project instead (see DEPLOY.md) - this
removes the dependency on a stranger's server without us having to
reverse-engineer NCAA's GraphQL persisted-query hashes ourselves. When
pointed at a private Cloud Run Service, requests are authenticated with a
Google-issued identity token scoped to that service's URL, fetched
automatically from the environment's credentials (works out of the box
inside a Cloud Run Job).

Shared between the men's and women's pipelines: pass sport="volleyball-men"
or sport="volleyball-women" to every function. Men's is a spring sport
(roughly January-May); women's is a fall sport (roughly August-December).
A daily "yesterday" pull will return zero rows for months at a time during
each sport's off-season, which is expected, not a bug.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from typing import Any

import pandas as pd
import requests

PUBLIC_DEMO_URL = "https://ncaa-api.henrygd.me"
BASE_URL = os.environ.get("NCAA_API_BASE_URL", PUBLIC_DEMO_URL)
COMPLETED_STATES = {"final"}
REQUEST_DELAY_SECONDS = 0.25  # stay well under the public demo's 5 req/sec rate limit

_id_token_cache: str | None = None


def _get_id_token(audience: str) -> str:
    """Fetch a Google-issued identity token scoped to a private Cloud Run
    service, using whatever credentials are available in the environment
    (the job's attached service account, when running on Cloud Run)."""
    global _id_token_cache
    if _id_token_cache is not None:
        return _id_token_cache

    import google.auth.transport.requests
    import google.oauth2.id_token

    auth_request = google.auth.transport.requests.Request()
    _id_token_cache = google.oauth2.id_token.fetch_id_token(auth_request, audience)
    return _id_token_cache


def _get(path: str) -> Any:
    headers = {}
    if BASE_URL != PUBLIC_DEMO_URL:
        # self-hosted private Cloud Run Service: authenticate with an
        # identity token scoped to the service's own URL
        headers["Authorization"] = f"Bearer {_get_id_token(BASE_URL)}"

    resp = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=30)
    resp.raise_for_status()
    if BASE_URL == PUBLIC_DEMO_URL:
        time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def _flatten_boxscore(game_id: str, game_date: str, boxscore: dict[str, Any]) -> list[dict[str, Any]]:
    # Normalize teamId to string on both sides of this join: in production
    # responses the `teams` array and `teamBoxscore` array don't reliably
    # agree on int vs string for the same field, which silently breaks a
    # bare dict lookup (every row falls back to the "not found" default,
    # producing a null team and a wrong home/away for every single row -
    # this happened in the first real backfill and wasn't caught by
    # mocked testing, which had consistently-typed IDs on both sides).
    teams_by_id = {str(t["teamId"]): t for t in boxscore.get("teams", [])}
    rows: list[dict[str, Any]] = []

    for team_box in boxscore.get("teamBoxscore", []):
        team_id = str(team_box.get("teamId"))
        team_info = teams_by_id.get(team_id, {})
        if not team_info:
            print(
                f"WARNING: no matching team for teamId={team_id!r} in game "
                f"{game_id}; known team ids: {list(teams_by_id.keys())!r}. "
                "team/home_away will be null/wrong for these rows.",
                file=sys.stderr,
            )
        team_abbr = team_info.get("name6Char")
        home_away = "home" if team_info.get("isHome") else "away"

        for p in team_box.get("playerStats", []):
            rows.append(
                {
                    "game_id": game_id,
                    "game_date": game_date,
                    "team": team_abbr,
                    "home_away": home_away,
                    "player_name": f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
                    "position": p.get("position"),
                    "starter": p.get("starter"),
                    "points": p.get("points"),
                    "kills": p.get("kills"),
                    "attack_errors": p.get("attackErrors"),
                    "attack_attempts": p.get("attackAttempts"),
                    "hitting_percentage": p.get("hittingPercentage"),
                    "assists": p.get("assists"),
                    "service_aces": p.get("serviceAces"),
                    "service_errors": p.get("serviceErrors"),
                    "serve_attempts": p.get("serveAttempts"),
                    "digs": p.get("digs"),
                    "reception_attempts": p.get("receptionAttempts"),
                    "reception_errors": p.get("receptionErrors"),
                    "block_solos": p.get("blockSolos"),
                    "block_assists": p.get("blockAssists"),
                    "total_blocks": p.get("totalBlocks"),
                }
            )
    return rows


def fetch_boxscores_for_date(date: str, sport: str, division: str = "d1") -> pd.DataFrame:
    """Fetch flattened player-level boxscore rows for every completed game on
    a date (YYYY-MM-DD). Returns an empty DataFrame on an off-day."""
    year, month, day = date.split("-")
    scoreboard = _get(f"/scoreboard/{sport}/{division}/{year}/{month}/{day}/all-conf")
    games = scoreboard.get("games", [])

    all_rows: list[dict[str, Any]] = []
    for g in games:
        game = g.get("game", {})
        if game.get("gameState") not in COMPLETED_STATES:
            continue
        game_id = game.get("gameID")
        if game_id is None:
            continue

        boxscore = _get(f"/game/{game_id}/boxscore")
        all_rows.extend(_flatten_boxscore(game_id, date, boxscore))

    return pd.DataFrame(all_rows)


def fetch_boxscore_range(start_date: str, end_date: str, sport: str, division: str = "d1") -> pd.DataFrame:
    """Fetch boxscore rows across an inclusive date range (YYYY-MM-DD each)."""
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)

    frames = []
    day = start
    while day <= end:
        frames.append(fetch_boxscores_for_date(day.isoformat(), sport, division))
        day += dt.timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_yesterday(sport: str, division: str = "d1") -> pd.DataFrame:
    """Convenience wrapper for the daily scheduled job: pull yesterday's games."""
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    return fetch_boxscores_for_date(yesterday, sport, division)
