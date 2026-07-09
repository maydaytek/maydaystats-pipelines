"""Parse a VolleyStation "Match Box Score" PDF (the per-player, per-match
report MLV/provolleyball.com links from its schedule-events API) into
structured rows.

Why this exists: provolleyball.com's own JSON API exposes team-level and
season-level stats, but never a per-player, per-match boxscore directly.
That data does exist, just not as JSON - every completed schedule-event's
`volleyStationMatch` include carries a `report` field pointing at a public,
unauthenticated PDF hosted on DigitalOcean Spaces (VolleyStation's own
export format, so the same fixed layout for every match in the league).
This module turns that PDF's extracted text back into rows.

Layout quirks this parser specifically works around:

1. Each player row prints a variable-length "points scored per set" grid
   (0-5 numbers, one per set the player scored in) before the real,
   always-present 14-column stat block (Attack Atts/K/E/Eff/K%, Assist
   Ast/BHE, Serve Ace/E, Pass GP/E, Dig #, Block +, Pts +). A player who
   scored zero points in every set has an empty grid instead of a run of
   placeholder dots, so different rows have different token counts before
   that fixed block. We don't need the per-set breakdown (the rest of this
   project's boxscore tables are match-level totals, not per-set), so we
   only ever keep the last 14 tokens on the line - whatever precedes them
   is the set-by-set grid, however long it happens to be for that player.

2. When a player's attack efficiency (Eff, a decimal like "1.000") sits
   directly next to their kill percentage (K%, e.g. "100%") with no other
   separator, the PDF's text layer sometimes has zero gap between them and
   they get extracted as one glued token ("1.000100%"). MERGE_FIX_RE splits
   these back into two tokens before anything else is parsed.

3. A "Team Total" row closes out each team's block. Since it's not a player
   row, it's parsed separately - and also gives us a free, automatic
   correctness check: summing every player's Atts/K/E/Ast/Ace/Digs/
   Blocks/Pts should equal that row exactly. validate_against_team_total
   does that comparison; a mismatch means something about this specific
   file's layout broke the assumptions above and the match should be
   flagged for manual review rather than trusted blindly.
"""
from __future__ import annotations

import re

STAT_COLS = [
    "attack_attempts", "kills", "attack_errors", "hitting_percentage",
    "kill_pct", "assists", "setting_errors", "service_aces",
    "service_errors", "good_passes", "reception_errors", "digs",
    "total_blocks", "points",
]

# columns that are meaningful to sum across a team's roster and compare
# against the printed "Team Total" row, as a parse-correctness check
SUMMABLE_COLS = [
    "attack_attempts", "kills", "attack_errors", "assists",
    "service_aces", "digs", "total_blocks", "points",
]

MERGE_FIX_RE = re.compile(r'(-?\d\.\d{3})(\d{1,3}%)')

HEADER_LINES = {
    "Set", "1 2 3 4 5", "Attack", "Atts K E Eff K%", "Assist", "Ast BHE",
    "Serve", "Ace E", "Pass", "GP E", "Dig", "#", "Block", "+", "Pts",
}


def _to_num(tok: str) -> float | None:
    if tok in (".", "-", ""):
        return None
    tok = tok.rstrip("%")
    try:
        return float(tok)
    except ValueError:
        return None


def _align_stats(tokens: list[str]) -> list[str]:
    """Return exactly 14 tokens. Extra leading tokens are the variable-
    length per-set points grid (drop them); a shortfall means a fully
    inactive player whose row prints fewer placeholder dots (left-pad with
    '.', which is safe since every value for that player is blank anyway)."""
    if len(tokens) >= 14:
        return tokens[-14:]
    return ["."] * (14 - len(tokens)) + tokens


# MLV/VolleyStation used at least two different box score PDF templates
# across the 2025-26 season: a "Match Box Score" layout (columns Atts K E
# Eff K% / Ast BHE / Ace E / GP E / # / + / +, "Team Total" row - what
# every function above this point was built and validated against) and an
# older "Match report" layout (columns Vote / Tot BP W-L / Tot Err Pts /
# Tot Err Pos% (Exc%) / Tot Err Blo Pts Pts% / BK Pts, "Players total"
# row). The two are NOT compatible: feeding the older layout through this
# parser doesn't fail loudly, it silently produces a full roster of
# players with completely wrong numbers in every column (things like a
# player's total_blocks reading 71, or negative attack attempts), because
# enough of the line shapes coincidentally still match. This exact string
# only ever appears in the newer layout's header, so its absence is the
# signal to bail out before parsing anything, rather than trust a
# checksum to catch column-shuffled garbage after the fact.
SUPPORTED_FORMAT_MARKER = "Atts K E Eff K%"


class UnsupportedFormatError(Exception):
    """Raised when the PDF text doesn't match the "Match Box Score"
    layout this parser was built for (e.g. it's the older "Match report"
    layout MLV used earlier in the 2025-26 season)."""


def parse_box_score(
    text: str, team1_name: str, team2_name: str
) -> tuple[list[dict], dict[str, dict]]:
    """Parse extracted PDF text into (player_rows, team_totals).

    player_rows: list of dicts with team, jersey, libero, last_name,
    first_name, and the STAT_COLS fields.
    team_totals: {team_name: {stat_col: value}} parsed from each "Team
    Total" row, for validate_against_team_total.

    Raises UnsupportedFormatError if the text doesn't look like the
    "Match Box Score" layout at all, rather than silently returning
    garbage rows built from misread columns.
    """
    if SUPPORTED_FORMAT_MARKER not in text:
        raise UnsupportedFormatError(
            f"text doesn't contain the expected header "
            f"{SUPPORTED_FORMAT_MARKER!r} - this looks like a different "
            "report template (e.g. the older 'Match report' layout), not "
            "the 'Match Box Score' layout this parser handles"
        )

    text = MERGE_FIX_RE.sub(r"\1 \2", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    team_names = {team1_name, team2_name}
    players: list[dict] = []
    team_totals: dict[str, dict] = {}
    current_team: str | None = None
    in_footer = False

    for line in lines:
        if line.startswith("Set Atts K E Eff SO% PS%"):
            in_footer = True
        if in_footer:
            continue
        if line in team_names:
            current_team = line
            continue
        if line in HEADER_LINES:
            continue
        if current_team is None:
            continue
        if line.startswith("Team Total"):
            tokens = line.split()[2:]
            stats = _align_stats(tokens)
            team_totals[current_team] = dict(
                zip(STAT_COLS, [_to_num(t) for t in stats])
            )
            continue

        m = re.match(r"^(\d+)\s+(.*)$", line)
        if not m:
            continue
        jersey, rest = m.group(1), m.group(2).split()
        if rest and rest[0].isdigit():
            # the "1 2 3 4 5" set-header line, misidentified as a jersey
            # row because it's all digits - not a real player
            continue

        is_libero = False
        if rest and rest[0] == "L":
            is_libero = True
            rest = rest[1:]
        if len(rest) < 2:
            continue

        last_name, first_name = rest[0], rest[1]
        stats = _align_stats(rest[2:])
        players.append(
            {
                "team": current_team,
                "jersey": jersey,
                "libero": is_libero,
                "last_name": last_name,
                "first_name": first_name,
                **{col: _to_num(v) for col, v in zip(STAT_COLS, stats)},
            }
        )

    return players, team_totals


def validate_against_team_total(
    players: list[dict], team_totals: dict[str, dict], team_name: str
) -> dict[str, dict]:
    """Compare summed player stats against the PDF's own printed team
    total row. Returns a dict of {col: {summed, expected, match}}. Any
    `match: False` means this file didn't parse cleanly and should be
    treated with suspicion rather than loaded silently."""
    team_players = [p for p in players if p["team"] == team_name]
    total = team_totals.get(team_name, {})
    checks = {}
    for col in SUMMABLE_COLS:
        summed = sum(p[col] for p in team_players if p[col] is not None)
        expected = total.get(col)
        checks[col] = {
            "summed": summed,
            "expected": expected,
            "match": expected is not None and abs(summed - expected) < 0.01,
        }
    return checks


def box_score_is_valid(players: list[dict], team_totals: dict[str, dict]) -> bool:
    """True if every team's summed stats match their printed team total."""
    teams = set(p["team"] for p in players) | set(team_totals.keys())
    for team in teams:
        checks = validate_against_team_total(players, team_totals, team)
        if not all(c["match"] for c in checks.values()):
            return False
    return True


# ---------------------------------------------------------------------------
# Legacy "Match report" layout
#
# MLV/VolleyStation switched to the "Match Box Score" layout above partway
# through the 2025-26 season. Earlier matches (roughly the first two-thirds
# of the regular season) use an older "Match report" layout with entirely
# different columns and no "Team Total" row at all (it's "Players total").
# It also tracks fewer stats: there's no Assist or Dig section anywhere in
# this layout, so assists/digs/setting_errors/good_passes are always None
# for a match parsed by this function - a real gap in these older games'
# data, not a parsing bug.
#
# Column groups, in the order they appear on the page: Vote (a single
# per-player rating, not summable/team-total-relevant), Points (Tot/BP/
# W-L), Serve (Tot/Err/Pts), Reception (Tot/Err/Pos%/Exc%), Attack (Tot/
# Err/Blo/Pts/Pts%), BK (Pts). Excluding Vote, that's a fixed 16-column
# block per player, same "variable prefix + fixed suffix" shape as the
# newer layout (the prefix here is 0-5 per-set values plus one Vote
# value), so the same "keep the last N tokens" strategy applies, just
# with N=16 instead of 14.
# ---------------------------------------------------------------------------

LEGACY_STAT_COLS = [
    "points_total", "points_bp", "points_wl", "serve_total", "serve_errors",
    "service_aces", "reception_total", "reception_errors",
    "reception_pos_pct", "reception_exc_pct", "attack_total",
    "attack_errors", "attack_blocked", "kills", "kill_pct", "total_blocks",
]

# points_total should equal kills + service_aces + total_blocks (every way
# a team scores a point, summed) - the internal check unique to this
# layout, on top of the usual sum-of-players-vs-printed-total check
LEGACY_SUMMABLE_COLS = [
    "points_total", "points_bp", "serve_total", "serve_errors",
    "service_aces", "reception_total", "reception_errors", "attack_total",
    "attack_errors", "attack_blocked", "kills", "total_blocks",
]

LEGACY_FORMAT_MARKER = "Tot BP W-L"
LEGACY_TOTAL_ROW_PREFIX = "Players total"


def _to_num_legacy(tok: str) -> float | None:
    if tok in (".", "-", ""):
        return None
    tok = tok.strip("()").rstrip("%")
    try:
        return float(tok)
    except ValueError:
        return None


def _align_stats_legacy(tokens: list[str]) -> list[str]:
    if len(tokens) >= 16:
        return tokens[-16:]
    return ["."] * (16 - len(tokens)) + tokens


def _matches_team_header_legacy(line: str, team_name: str) -> bool:
    """True for a line that starts a team's roster block: either the team
    name alone, or the team name immediately followed by "Set" (a text-
    extraction quirk where the team name and the first roster header line
    sometimes merge onto one line and sometimes don't, seen even within
    the same document - team 1 rendered "Indy Ignite Set" as one line
    while team 2 rendered "Omaha Supernovas" and "Set" as two)."""
    if not line.startswith(team_name):
        return False
    rest = line[len(team_name):].strip()
    return rest in ("", "Set")


def parse_box_score_legacy(
    text: str, team1_name: str, team2_name: str
) -> tuple[list[dict], dict[str, dict]]:
    """Parse the older "Match report" layout. Same return shape as
    parse_box_score, but with LEGACY_STAT_COLS fields instead of
    STAT_COLS - these two layouts don't track the same stats, so callers
    need to handle them as distinct schemas (fetch.py maps what overlaps
    onto the shared boxscores table and leaves the rest null).

    Raises UnsupportedFormatError if the text doesn't match this layout
    either (e.g. it's neither known template).
    """
    if LEGACY_FORMAT_MARKER not in text:
        raise UnsupportedFormatError(
            f"text doesn't contain the expected header "
            f"{LEGACY_FORMAT_MARKER!r} - not the legacy 'Match report' "
            "layout either"
        )

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    players: list[dict] = []
    team_totals: dict[str, dict] = {}
    current_team: str | None = None
    total_rows_seen = 0
    in_footer = False

    for line in lines:
        if in_footer:
            continue
        if _matches_team_header_legacy(line, team1_name):
            current_team = team1_name
            continue
        if _matches_team_header_legacy(line, team2_name):
            current_team = team2_name
            continue
        if current_team is None:
            continue
        if line.startswith(LEGACY_TOTAL_ROW_PREFIX):
            tokens = line.split()[2:]
            stats = _align_stats_legacy(tokens)
            team_totals[current_team] = dict(
                zip(LEGACY_STAT_COLS, [_to_num_legacy(t) for t in stats])
            )
            total_rows_seen += 1
            if total_rows_seen >= 2:
                # everything after both teams' totals is footer
                # (match-level comparison tables, the stat-code legend,
                # branding) - not player data, stop here
                in_footer = True
            continue

        m = re.match(r"^(\d+)\s+(.*)$", line)
        if not m:
            continue
        jersey, rest = m.group(1), m.group(2).split()
        if rest and rest[0].isdigit():
            continue  # the "1 2 3 4 5" set-header line, not a real player

        is_libero = False
        if rest and rest[0] == "L":
            is_libero = True
            rest = rest[1:]
        if len(rest) < 2:
            continue

        last_name, first_name = rest[0], rest[1]
        stats = _align_stats_legacy(rest[2:])
        players.append(
            {
                "team": current_team,
                "jersey": jersey,
                "libero": is_libero,
                "last_name": last_name,
                "first_name": first_name,
                **{
                    col: _to_num_legacy(v)
                    for col, v in zip(LEGACY_STAT_COLS, stats)
                },
            }
        )

    return players, team_totals


def validate_against_team_total_legacy(
    players: list[dict], team_totals: dict[str, dict], team_name: str
) -> dict[str, dict]:
    team_players = [p for p in players if p["team"] == team_name]
    total = team_totals.get(team_name, {})
    checks = {}
    for col in LEGACY_SUMMABLE_COLS:
        summed = sum(p[col] for p in team_players if p[col] is not None)
        expected = total.get(col)
        checks[col] = {
            "summed": summed,
            "expected": expected,
            "match": expected is not None and abs(summed - expected) < 0.01,
        }
    return checks


def box_score_is_valid_legacy(
    players: list[dict], team_totals: dict[str, dict]
) -> bool:
    teams = set(p["team"] for p in players) | set(team_totals.keys())
    for team in teams:
        checks = validate_against_team_total_legacy(players, team_totals, team)
        if not all(c["match"] for c in checks.values()):
            return False
    return True
