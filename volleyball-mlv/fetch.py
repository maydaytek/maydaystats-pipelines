"""Fetch MLV (Major League Volleyball, provolleyball.com) box scores.

provolleyball.com's own JSON API (built by WMT Digital on top of
VolleyStation's stats software) is first-party and needs no auth - same
tier of provenance as MLB's Statcast or the NHL API this project already
uses. Unlike those two, though, it never exposes a per-player, per-match
boxscore as JSON directly, only team-level and season-level aggregates.
That per-player data does exist: every completed schedule-event's
`volleyStationMatch` include carries a `report` field, a public PDF
(VolleyStation's own "Match Box Score" export) hosted on DigitalOcean
Spaces, no auth needed. This module fetches the schedule, downloads each
match's PDF, and hands the extracted text to parser.py.

There is no "yesterday" concept here the way the NCAA/baseball/hockey
pipelines use one - MLV plays 2-4 games a week, not most days. Instead,
main.py passes in the set of volley_station_match_id values already in
BigQuery, and fetch_new_matches() just skips anything already loaded. That
makes a full backfill and a normal incremental run the same code path.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from typing import Any

import pandas as pd
import requests

from parser import box_score_is_valid, parse_box_score

BASE_URL = "https://provolleyball.com/api"
REQUEST_TIMEOUT = 30
EXTRACT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_extract_pdf_text.py")
EXTRACT_TIMEOUT = 60


def _get(path: str, params: dict | None = None) -> Any:
    resp = requests.get(
        f"{BASE_URL}{path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def list_season_ids() -> list[int]:
    """Every season_id the API knows about (regular seasons only have
    real schedule-events once they start - an upcoming season with no
    games yet just contributes zero rows, same idea as the NCAA
    pipelines' off-season months)."""
    data = _get("/seasons", {"per_page": 50})
    return [s["id"] for s in data.get("data", [])]


def list_schedule_events(season_id: int) -> list[dict]:
    """Every completed schedule-event for a season, with its
    volleyStationMatch relation already included (report/scoresheet URLs,
    set scores, etc. come along for free in the same call - no need for a
    second request per match)."""
    events: list[dict] = []
    page = 1
    while True:
        data = _get(
            "/schedule-events",
            {
                "filter[season_id]": season_id,
                "filter[status]": "completed",
                "include[0]": "volleyStationMatch",
                "per_page": 100,
                "page": page,
            },
        )
        events.extend(data.get("data", []))
        meta = data.get("meta", {})
        if page >= meta.get("last_page", page):
            break
        page += 1
    return events


PDF_DOWNLOAD_RETRIES = 3
PDF_DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
PDF_DOWNLOAD_PACING_SECONDS = 0.3


def _download_pdf_bytes(url: str) -> bytes:
    """Download a report PDF, retrying on anything that looks like a
    truncated/corrupted response.

    Backfilling a full season means ~100+ of these downloads back-to-back
    with no pacing between them, which turned out to occasionally produce
    truncated responses from the CDN that `requests` doesn't always flag
    as an HTTP error (status 200, just fewer bytes than the real file) -
    that corrupted a real download into a file pdfplumber could open but
    not extract usable text from, which is what actually caused most of
    the checksum failures and zero-player parses in the first backfill
    attempt, not anything about the parser or the container's Python
    environment (both were cleared by testing this exact file in
    isolation, which downloaded and parsed perfectly every time). A real
    PDF always starts with the "%PDF-" magic bytes; anything that doesn't
    is treated as a bad download and retried rather than handed to
    pdfplumber and silently mis-parsed.
    """
    last_exc: Exception | None = None
    for attempt in range(1, PDF_DOWNLOAD_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            content = resp.content
            if not content.startswith(b"%PDF-"):
                raise ValueError(
                    f"response doesn't look like a PDF (got {len(content)} "
                    f"bytes, starts with {content[:16]!r})"
                )
            declared_length = resp.headers.get("Content-Length")
            if declared_length is not None and int(declared_length) != len(content):
                raise ValueError(
                    f"truncated download: Content-Length said "
                    f"{declared_length} bytes, got {len(content)}"
                )
            return content
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_exc = exc
            print(
                f"WARNING: PDF download attempt {attempt}/{PDF_DOWNLOAD_RETRIES} "
                f"failed for {url}: {exc}",
                file=sys.stderr,
            )
            time.sleep(PDF_DOWNLOAD_RETRY_DELAY_SECONDS)
    raise last_exc  # type: ignore[misc]


def _extract_pdf_text(content: bytes) -> str:
    """Run text extraction in its own subprocess rather than in-process.

    Processing ~100 PDFs back-to-back in one long-lived interpreter
    produced deterministically corrupted text for a consistent subset of
    files, every run, in the same order - even after ruling out download
    corruption, parser bugs, the container's Python version, and the OS/
    platform (see _extract_pdf_text.py's docstring for the full trail).
    The remaining explanation is pdfminer.six's internal font/CMap cache
    leaking across documents that reuse the same internal font name
    (which these all do, being generated by the same VolleyStation
    template). A fresh interpreter per file has no cache to leak from.
    """
    result = subprocess.run(
        [sys.executable, EXTRACT_SCRIPT],
        input=content,
        capture_output=True,
        timeout=EXTRACT_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PDF extraction subprocess failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:500]}"
        )
    return result.stdout.decode("utf-8", errors="replace")


def _download_pdf_text(url: str) -> str:
    content = _download_pdf_bytes(url)
    time.sleep(PDF_DOWNLOAD_PACING_SECONDS)  # don't hammer the CDN
    return _extract_pdf_text(content)


def _match_row(event: dict, vsm: dict, checksum_ok: bool) -> dict:
    return {
        "schedule_event_id": event["id"],
        "volley_station_match_id": event.get("volley_station_match_id"),
        "season_id": event.get("season_id"),
        "game_date": event.get("start_datetime"),
        "location": event.get("location"),
        "first_team_id": event.get("first_team_id"),
        "first_team_name": event.get("first_team_name"),
        "first_team_score": event.get("first_team_score"),
        "second_team_id": event.get("second_team_id"),
        "second_team_name": event.get("second_team_name"),
        "second_team_score": event.get("second_team_score"),
        "won_set_home": vsm.get("won_set_home"),
        "won_set_guest": vsm.get("won_set_guest"),
        "checksum_ok": checksum_ok,
    }


def _box_row(event: dict, match_id: int | None, player: dict) -> dict:
    return {
        "schedule_event_id": event["id"],
        "volley_station_match_id": match_id,
        "game_date": event.get("start_datetime"),
        "season_id": event.get("season_id"),
        "team": player["team"],
        "jersey": player["jersey"],
        "libero": player["libero"],
        "player_name": f"{player['first_name']} {player['last_name']}".strip(),
        "last_name": player["last_name"],
        "first_name": player["first_name"],
        "attack_attempts": player["attack_attempts"],
        "kills": player["kills"],
        "attack_errors": player["attack_errors"],
        "hitting_percentage": player["hitting_percentage"],
        "kill_pct": player["kill_pct"],
        "assists": player["assists"],
        "setting_errors": player["setting_errors"],
        "service_aces": player["service_aces"],
        "service_errors": player["service_errors"],
        "good_passes": player["good_passes"],
        "reception_errors": player["reception_errors"],
        "digs": player["digs"],
        "total_blocks": player["total_blocks"],
        "points": player["points"],
    }


def fetch_new_matches(
    season_ids: list[int] | None = None,
    already_loaded_match_ids: set[int] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch every completed match not already in already_loaded_match_ids
    across the given seasons (default: every season the API knows about).
    Returns (matches_df, boxscores_df)."""
    already_loaded_match_ids = already_loaded_match_ids or set()
    if season_ids is None:
        season_ids = list_season_ids()

    match_rows: list[dict] = []
    box_rows: list[dict] = []
    seen_match_ids: set[int] = set()

    for season_id in season_ids:
        events = list_schedule_events(season_id)
        for event in events:
            vsm = event.get("volley_station_match")
            if not vsm or not vsm.get("report"):
                continue  # no boxscore PDF available for this event

            match_id = event.get("volley_station_match_id")
            if match_id in already_loaded_match_ids or match_id in seen_match_ids:
                # Already loaded on a previous run, or a duplicate
                # schedule_event pointing at the same underlying match -
                # this happens for postponed-then-rescheduled games, which
                # can leave two schedule_event rows sharing one
                # volley_station_match_id.
                continue
            seen_match_ids.add(match_id)

            t1 = event.get("first_team_name")
            t2 = event.get("second_team_name")
            if not t1 or not t2:
                print(
                    f"WARNING: schedule_event {event['id']} missing a team "
                    "name, skipping",
                    file=sys.stderr,
                )
                continue

            try:
                content = _download_pdf_bytes(vsm["report"])
                content_hash = hashlib.sha256(content).hexdigest()[:16]
                text = _extract_pdf_text(content)
                time.sleep(PDF_DOWNLOAD_PACING_SECONDS)
                print(
                    f"DEBUG event {event['id']}: downloaded {len(content)} "
                    f"bytes (sha256:{content_hash}), extracted "
                    f"{len(text)} chars",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001 - want to see everything during this debug pass
                print(
                    f"WARNING: could not download/extract report PDF for "
                    f"schedule_event {event['id']}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue

            players, totals = parse_box_score(text, t1, t2)
            if not players:
                print(
                    f"WARNING: report PDF for schedule_event {event['id']} "
                    "parsed to zero players, skipping",
                    file=sys.stderr,
                )
                continue

            checksum_ok = box_score_is_valid(players, totals)
            if not checksum_ok:
                print(
                    f"WARNING: schedule_event {event['id']} ({t1} vs {t2}) "
                    "failed the team-total checksum - loading anyway with "
                    "checksum_ok=False so it can be filtered out or "
                    "reviewed manually",
                    file=sys.stderr,
                )

            match_rows.append(_match_row(event, vsm, checksum_ok))
            for p in players:
                box_rows.append(_box_row(event, match_id, p))

    return pd.DataFrame(match_rows), pd.DataFrame(box_rows)
