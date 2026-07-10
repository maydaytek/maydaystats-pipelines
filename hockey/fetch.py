"""Fetch NHL boxscore data via nhl-api-py (wraps the NHL's undocumented
public api-web.nhle.com endpoints - the same source MoneyPuck and Natural
Stat Trick build on).

Field-name mapping: the NHL API is explicitly undocumented and has no
official schema guarantee, so the field names in
`_flatten_skaters`/`_flatten_goalies` started as a best-effort mapping,
built and tested in this project's sandbox against a hand-built mock
payload (outbound requests to api-web.nhle.com are blocked there). That
mapping has since been validated against a full live 2025-26 season
backfill (regular season and playoffs) in BigQuery. If the NHL changes
its schema in a future season, the symptom is the same: a field coming
back null across the board in a run's logs means the key name needs
adjusting to match what the live API currently returns.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
from nhlpy import NHLClient

COMPLETED_STATES = {"OFF", "FINAL", "OFFICIAL"}


def _name(value: Any) -> Any:
    """NHL API names often come back as {'default': 'C. McDavid'} - unwrap that."""
    if isinstance(value, dict):
        return value.get("default")
    return value


def _flatten_skaters(
    game_id: Any,
    game_date: str,
    team_abbr: Any,
    home_away: str,
    players: list[dict[str, Any]],
    position_group: str,
) -> list[dict[str, Any]]:
    rows = []
    for p in players:
        rows.append(
            {
                "game_id": game_id,
                "game_date": game_date,
                "team": team_abbr,
                "home_away": home_away,
                "position_group": position_group,
                "player_id": p.get("playerId"),
                "player_name": _name(p.get("name")),
                "position": p.get("position"),
                "goals": p.get("goals"),
                "assists": p.get("assists"),
                "points": p.get("points"),
                "plus_minus": p.get("plusMinus"),
                "penalty_minutes": p.get("pim"),
                "shots_on_goal": p.get("sog"),
                "hits": p.get("hits"),
                "blocked_shots": p.get("blockedShots"),
                "toi": p.get("toi"),
            }
        )
    return rows


def _flatten_goalies(
    game_id: Any,
    game_date: str,
    team_abbr: Any,
    home_away: str,
    goalies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for g in goalies:
        rows.append(
            {
                "game_id": game_id,
                "game_date": game_date,
                "team": team_abbr,
                "home_away": home_away,
                "position_group": "goalie",
                "player_id": g.get("playerId"),
                "player_name": _name(g.get("name")),
                "position": "G",
                "shots_against": g.get("shotsAgainst"),
                "saves": g.get("saves"),
                "goals_against": g.get("goalsAgainst"),
                "save_pctg": g.get("savePctg"),
                "decision": g.get("decision"),
                "toi": g.get("toi"),
            }
        )
    return rows


def fetch_boxscores_for_date(date: str, client: NHLClient | None = None) -> pd.DataFrame:
    """Fetch flattened player-level boxscore rows for every completed game on
    a date (YYYY-MM-DD). Returns an empty DataFrame on an off-day."""
    client = client or NHLClient()
    schedule = client.schedule.daily_schedule(date=date)
    games = schedule.get("games", [])

    all_rows: list[dict[str, Any]] = []
    for game in games:
        game_id = game.get("id")
        if game_id is None or game.get("gameState") not in COMPLETED_STATES:
            continue

        boxscore = client.game_center.boxscore(game_id=str(game_id))
        stats = boxscore.get("playerByGameStats", {})

        for home_away, team_key in (("away", "awayTeam"), ("home", "homeTeam")):
            team_stats = stats.get(team_key, {})
            team_abbr = boxscore.get(team_key, {}).get("abbrev")
            all_rows.extend(
                _flatten_skaters(game_id, date, team_abbr, home_away, team_stats.get("forwards", []), "forward")
            )
            all_rows.extend(
                _flatten_skaters(game_id, date, team_abbr, home_away, team_stats.get("defense", []), "defense")
            )
            all_rows.extend(
                _flatten_goalies(game_id, date, team_abbr, home_away, team_stats.get("goalies", []))
            )

    return pd.DataFrame(all_rows)


def fetch_boxscore_range(start_date: str, end_date: str, client: NHLClient | None = None) -> pd.DataFrame:
    """Fetch boxscore rows across an inclusive date range (YYYY-MM-DD each)."""
    client = client or NHLClient()
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)

    frames = []
    day = start
    while day <= end:
        frames.append(fetch_boxscores_for_date(day.isoformat(), client=client))
        day += dt.timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_yesterday() -> pd.DataFrame:
    """Convenience wrapper: pull a single day, yesterday. Kept for manual
    testing; the scheduled job uses fetch_recent() instead, see below."""
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    return fetch_boxscores_for_date(yesterday)


def fetch_recent(lookback_days: int = 3) -> pd.DataFrame:
    """Self-healing convenience wrapper for the daily scheduled job: pull a
    rolling window of the last `lookback_days` days (ending yesterday)
    instead of just yesterday alone.

    Added after the baseball pipeline's identical single-day pull was
    found returning 0 rows on real game days, because Statcast's source
    data lagged behind the early-morning scheduled run. The NHL API
    likely isn't affected the same way, but the fix is cheap and makes
    every one of these daily pipelines self-healing against any future
    lag or a single missed scheduler run: a day missed on one run gets
    picked up automatically on the next, instead of staying empty
    forever. bigquery_loader.load_dataframe dedups against game_date
    already in the table, so re-fetching overlapping days here is safe.
    """
    end = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    return fetch_boxscore_range(start, end)
