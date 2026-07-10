"""Fetch season-level MLB batting, pitching, and team stats via MLB's own
Stats API (statsapi.mlb.com) - a real first-party JSON API, not a scrape.

This pipeline originally used pybaseball's FanGraphs wrapper, but FanGraphs
added a Cloudflare bot wall that started returning 403s on every request -
a known, unfixable-client-side issue (see
https://github.com/jldbc/pybaseball/issues/479, where multiple users hit
the exact same error with no working fix). MLB's own API is what MLB.com's
own stats pages call to render themselves, so there's no bot wall to run
into, and it comes with real player names attached, same as before.
"""
from __future__ import annotations

import pandas as pd
import requests

BASE_URL = "https://statsapi.mlb.com/api/v1"
MLB_SPORT_ID = 1  # 1 = MLB; other sportId values cover MiLB levels
LIMIT = 2000  # comfortably above any realistic season roster-plus-callups count


def _get(path: str, **params) -> dict:
    resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _flatten_player_splits(payload: dict) -> pd.DataFrame:
    """Flatten the nested stats[0].splits player-level response shape into
    one row per player, with stat fields alongside player/team metadata."""
    stats = payload.get("stats") or [{}]
    splits = stats[0].get("splits", [])
    rows = []
    for split in splits:
        row = dict(split.get("stat", {}))
        player = split.get("player", {})
        team = split.get("team", {})
        league = split.get("league", {})
        position = split.get("position", {})
        row["player_id"] = player.get("id")
        row["player_name"] = player.get("fullName")
        row["team_id"] = team.get("id")
        row["team_name"] = team.get("name")
        row["league_name"] = league.get("name")
        row["position"] = position.get("abbreviation")
        row["rank"] = split.get("rank")
        rows.append(row)
    return pd.DataFrame(rows)


def _flatten_team_splits(payload: dict) -> pd.DataFrame:
    """Same idea as _flatten_player_splits, for the team-level endpoint
    (no player/position fields, just team + stat)."""
    stats = payload.get("stats") or [{}]
    splits = stats[0].get("splits", [])
    rows = []
    for split in splits:
        row = dict(split.get("stat", {}))
        team = split.get("team", {})
        row["team_id"] = team.get("id")
        row["team_name"] = team.get("name")
        row["rank"] = split.get("rank")
        rows.append(row)
    return pd.DataFrame(rows)


def fetch_batting(season: int) -> pd.DataFrame:
    """All batters with at least one plate appearance this season - the
    default without playerPool=ALL would only return FanGraphs-style
    "qualified" regulars, which excludes part-timers and recent call-ups."""
    payload = _get(
        "stats",
        stats="season",
        group="hitting",
        season=season,
        sportId=MLB_SPORT_ID,
        playerPool="ALL",
        limit=LIMIT,
    )
    return _flatten_player_splits(payload)


def fetch_pitching(season: int) -> pd.DataFrame:
    """Same idea as fetch_batting, for pitchers (includes relievers and
    spot starters, not just qualified starters)."""
    payload = _get(
        "stats",
        stats="season",
        group="pitching",
        season=season,
        sportId=MLB_SPORT_ID,
        playerPool="ALL",
        limit=LIMIT,
    )
    return _flatten_player_splits(payload)


def fetch_team_batting(season: int) -> pd.DataFrame:
    """One row per team's aggregate batting stats for the season."""
    payload = _get(
        "teams/stats",
        stats="season",
        group="hitting",
        season=season,
        sportId=MLB_SPORT_ID,
    )
    return _flatten_team_splits(payload)


def fetch_team_pitching(season: int) -> pd.DataFrame:
    """One row per team's aggregate pitching stats for the season."""
    payload = _get(
        "teams/stats",
        stats="season",
        group="pitching",
        season=season,
        sportId=MLB_SPORT_ID,
    )
    return _flatten_team_splits(payload)
