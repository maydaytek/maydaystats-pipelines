"""Fetch MLB Statcast data via pybaseball.

pybaseball wraps Baseball Savant's public Statcast CSV export, so this is
free, unauthenticated, and pitch-level (release speed, spin rate, exit
velocity, launch angle, etc.) for every regular-season and postseason game.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from pybaseball import statcast


def fetch_statcast_range(start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch raw Statcast pitch-level data for an inclusive date range.

    Args:
        start_date: 'YYYY-MM-DD'
        end_date: 'YYYY-MM-DD'

    Returns:
        A DataFrame of pitch-level rows (empty DataFrame if nothing found,
        e.g. an off-day with no games).
    """
    df = statcast(start_dt=start_date, end_dt=end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def fetch_yesterday() -> pd.DataFrame:
    """Convenience wrapper: pull a single day, yesterday. Kept for manual
    testing; the scheduled job uses fetch_recent() instead, see below."""
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    return fetch_statcast_range(yesterday, yesterday)


def fetch_recent(lookback_days: int = 3) -> pd.DataFrame:
    """Self-healing convenience wrapper for the daily scheduled job: pull a
    rolling window of the last `lookback_days` days (ending yesterday)
    instead of just yesterday alone.

    Statcast's search export can lag a few hours behind when a game
    actually finished, so a strict single-day "yesterday" pull can come
    back empty even though real games were played - discovered when the
    9am UTC scheduled run kept returning 0 rows for days that definitely
    had games. Widening the window means a day missed on one run gets
    picked up automatically on the next one, rather than staying empty
    forever. bigquery_loader.load_dataframe dedups against game_date
    already in the table, so re-fetching overlapping days here is safe.
    """
    end = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    return fetch_statcast_range(start, end)
