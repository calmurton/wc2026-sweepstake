#!/usr/bin/env python3
"""
Fetch World Cup scores + top scorers from football-data.org and write them
to ``data/`` as JSON files the dashboard can consume.

Required env: FOOTBALL_DATA_TOKEN (free tier OK — World Cup is included).

Output files:
  data/scores.json        — { "<fixtureId>": { "h": int, "a": int } }
  data/top-scorers.json   — [ { "name": str, "team": str, "goals": int }, ... ]
  data/last-updated.json  — { "timestamp": ISO, "matchesFound": N, "scorersFound": N }

The script is intentionally tolerant of failure: if the scorers endpoint errors
we still write scores; if the API is unreachable the existing files are left
untouched so the dashboard keeps showing the last good data.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"
TIMEOUT = 30

TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN")
if not TOKEN:
    print("ERROR: FOOTBALL_DATA_TOKEN environment variable not set", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Team name normalisation ──────────────────────────────────────────────
# football-data.org's names sometimes differ from ours. Map both ways.
TEAM_ALIASES = {
    # API name → our canonical name
    "United States": "USA",
    "USA": "USA",
    "US": "USA",
    "Czech Republic": "Czech Republic",
    "Czechia": "Czech Republic",
    "Türkiye": "Turkey",
    "Turkey": "Turkey",
    "Korea Republic": "South Korea",
    "Republic of Korea": "South Korea",
    "South Korea": "South Korea",
    "IR Iran": "Iran",
    "Iran": "Iran",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Cabo Verde": "Cape Verde",
    "Cape Verde": "Cape Verde",
    "Cape Verde Islands": "Cape Verde",
    "DR Congo": "DR Congo",
    "Congo DR": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",
    "Democratic Republic of Congo": "DR Congo",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Ivory Coast": "Ivory Coast",
    "Curaçao": "Curaçao",
    "Curacao": "Curaçao",
    "Saudi Arabia": "Saudi Arabia",
    "KSA": "Saudi Arabia",
    "New Zealand": "New Zealand",
    "South Africa": "South Africa",
}


def normalize(name: str) -> str:
    return TEAM_ALIASES.get(name, name)


def api_get(endpoint: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{endpoint}",
        headers={"X-Auth-Token": TOKEN, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


# ── Load our canonical fixtures (for API match → fixture ID mapping) ─────
FIXTURES_FILE = DATA_DIR / "fixtures.json"
if not FIXTURES_FILE.exists():
    print(f"ERROR: {FIXTURES_FILE} missing — cannot map API matches", file=sys.stderr)
    sys.exit(1)

with FIXTURES_FILE.open(encoding="utf-8") as f:
    OUR_FIXTURES = json.load(f)


def map_to_fixture_id(home_api: str, away_api: str, utc_kickoff: str):
    """Try date+pair first, then fall back to just the team pair."""
    h = normalize(home_api)
    a = normalize(away_api)
    utc_date = utc_kickoff[:10] if utc_kickoff else ""
    # Exact match: same teams, same UTC calendar date
    for fx in OUR_FIXTURES:
        if fx["home"] == h and fx["away"] == a and fx["kickoffUTC"][:10] == utc_date:
            return fx["id"]
    # Loose match: same team pair only (kickoff may have shifted by a day across timezones)
    candidates = [fx for fx in OUR_FIXTURES if fx["home"] == h and fx["away"] == a]
    if len(candidates) == 1:
        return candidates[0]["id"]
    # Reverse pair fallback (in case API has home/away swapped vs our data)
    candidates = [fx for fx in OUR_FIXTURES if fx["home"] == a and fx["away"] == h]
    if len(candidates) == 1:
        return candidates[0]["id"]
    return None


def fetch_scores() -> dict:
    """Return { fixtureIdStr: { 'h': int, 'a': int } } for matches with a score."""
    try:
        data = api_get(f"/competitions/{COMPETITION_CODE}/matches")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"ERROR: matches endpoint failed: {e}", file=sys.stderr)
        sys.exit(2)

    scores = {}
    unmatched = []
    for m in data.get("matches", []):
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        utc = m.get("utcDate", "")
        status = m.get("status", "")
        s = m.get("score", {})

        # Debug: print raw score data for any non-scheduled match
        if status not in ("SCHEDULED", "TIMED"):
            ft_d = s.get("fullTime", {})
            rt_d = s.get("regularTime", {})
            ht_d = s.get("halfTime", {})
            print(f"  [{status}] {home} vs {away} | fullTime={ft_d} regularTime={rt_d} halfTime={ht_d}", file=sys.stderr)

        fx_id = map_to_fixture_id(home, away, utc)
        if fx_id is None:
            unmatched.append(f"{home} vs {away} ({utc[:10]})")
            continue
        ft = s.get("fullTime", {})
        h, a = ft.get("home"), ft.get("away")

        if h is None or a is None:
            if status in ("IN_PLAY", "PAUSED"):
                # Live — best available partial score
                ht = s.get("halfTime", {})
                h, a = ht.get("home"), ht.get("away")
            elif status == "FINISHED":
                # Free-tier lag: fullTime not yet populated despite match being over.
                # Try regularTime (after 90 mins, before AET), then halfTime as last resort.
                rt = s.get("regularTime", {})
                h, a = rt.get("home"), rt.get("away")
                if h is None or a is None:
                    ht = s.get("halfTime", {})
                    h, a = ht.get("home"), ht.get("away")
                if isinstance(h, int) and isinstance(a, int):
                    print(f"⚠ FINISHED match {home} vs {away} used fallback score (fullTime not yet populated)", file=sys.stderr)
        if isinstance(h, int) and isinstance(a, int):
            scores[str(fx_id)] = {"h": h, "a": a}

    if unmatched:
        print(f"⚠ {len(unmatched)} matches could not be mapped to our fixtures:", file=sys.stderr)
        for u in unmatched[:20]:
            print(f"  - {u}", file=sys.stderr)
        if len(unmatched) > 20:
            print(f"  ... and {len(unmatched) - 20} more", file=sys.stderr)
    return scores


def fetch_scorers() -> list:
    """Return top scorers as [{ name, team, goals }, ...] sorted by goals desc."""
    try:
        data = api_get(f"/competitions/{COMPETITION_CODE}/scorers?limit=25")
    except Exception as e:
        print(f"⚠ scorers endpoint failed (non-fatal): {e}", file=sys.stderr)
        return []

    out = []
    for s in data.get("scorers", []):
        player = s.get("player", {})
        team = s.get("team", {})
        goals = s.get("goals")
        if not goals or not player.get("name") or not team.get("name"):
            continue
        out.append({
            "name": player["name"],
            "team": normalize(team["name"]),
            "goals": goals,
        })
    # API returns sorted by goals desc but resort defensively
    out.sort(key=lambda p: (-p["goals"], p["name"]))
    return out


def main() -> int:
    print(f"Fetching from {API_BASE}/competitions/{COMPETITION_CODE} …")
    scores = fetch_scores()
    scorers = fetch_scorers()

    (DATA_DIR / "scores.json").write_text(
        json.dumps(scores, indent=2, sort_keys=True) + "\n"
    )
    (DATA_DIR / "top-scorers.json").write_text(
        json.dumps(scorers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (DATA_DIR / "last-updated.json").write_text(
        json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "matchesFound": len(scores),
            "scorersFound": len(scorers),
        }, indent=2) + "\n"
    )
    print(f"✓ Wrote {len(scores)} match scores, {len(scorers)} scorers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
