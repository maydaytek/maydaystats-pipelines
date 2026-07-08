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
    """Convenience wrapper for the daily scheduled job: pull yesterday's games."""
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    return fetch_statcast_range(yesterday, yesterday)
