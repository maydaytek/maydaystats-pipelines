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
