#!/usr/bin/env python3
"""
Fetch World Cup scores + top scorers from football-data.org and write them
to ``data/`` as JSON files the dashboard can consume.

Required env: FOOTBALL_DATA_TOKEN (free tier OK — World Cup is included).

Output files:
  data/scores.json        — { "<fixtureId>": { "h": int, "a": int } }
  data/top-scorers.json   — [ { "name": str, "team": str, "goals": int }, ... ]
  data/knockout.json      — { "fixtures": [...], "winner": str|null, "runnerup": str|null }
  data/last-updated.json  — { "timestamp": ISO, "matchesFound": N, "scorersFound": N }
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
TEAM_ALIASES = {
    "United States": "USA", "USA": "USA", "US": "USA",
    "Czech Republic": "Czech Republic", "Czechia": "Czech Republic",
    "Türkiye": "Turkey", "Turkey": "Turkey",
    "Korea Republic": "South Korea", "Republic of Korea": "South Korea", "South Korea": "South Korea",
    "IR Iran": "Iran", "Iran": "Iran",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Cabo Verde": "Cape Verde", "Cape Verde": "Cape Verde", "Cape Verde Islands": "Cape Verde",
    "DR Congo": "DR Congo", "Congo DR": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",
    "Democratic Republic of Congo": "DR Congo",
    "Côte d'Ivoire": "Ivory Coast", "Cote d'Ivoire": "Ivory Coast", "Ivory Coast": "Ivory Coast",
    "Curaçao": "Curaçao", "Curacao": "Curaçao",
    "Saudi Arabia": "Saudi Arabia", "KSA": "Saudi Arabia",
    "New Zealand": "New Zealand", "South Africa": "South Africa",
}

# ── Knockout stage display names ─────────────────────────────────────────
KNOCKOUT_STAGES = {
    "LAST_32":       "Round of 32",
    "LAST_16":       "Round of 16",
    "QUARTER_FINALS": "Quarter-finals",
    "SEMI_FINALS":   "Semi-finals",
    "THIRD_PLACE":   "Third Place",
    "FINAL":         "Final",
}

# Stage keys that are part of the group stage (skip for knockout processing)
GROUP_STAGE_KEYS = {"GROUP_STAGE", "FIRST_STAGE", "PRELIMINARY_ROUND"}


def normalize(name: str) -> str:
    if not name:
        return name
    return TEAM_ALIASES.get(name, name)


def api_get(endpoint: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{endpoint}",
        headers={"X-Auth-Token": TOKEN, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


# ── Load our canonical group-stage fixtures ──────────────────────────────
FIXTURES_FILE = DATA_DIR / "fixtures.json"
if not FIXTURES_FILE.exists():
    print(f"ERROR: {FIXTURES_FILE} missing — cannot map API matches", file=sys.stderr)
    sys.exit(1)

with FIXTURES_FILE.open(encoding="utf-8") as f:
    OUR_FIXTURES = json.load(f)


def map_to_fixture_id(home_api: str, away_api: str, utc_kickoff: str):
    h = normalize(home_api)
    a = normalize(away_api)
    utc_date = utc_kickoff[:10] if utc_kickoff else ""
    for fx in OUR_FIXTURES:
        if fx["home"] == h and fx["away"] == a and fx["kickoffUTC"][:10] == utc_date:
            return fx["id"]
    candidates = [fx for fx in OUR_FIXTURES if fx["home"] == h and fx["away"] == a]
    if len(candidates) == 1:
        return candidates[0]["id"]
    candidates = [fx for fx in OUR_FIXTURES if fx["home"] == a and fx["away"] == h]
    if len(candidates) == 1:
        return candidates[0]["id"]
    return None


def extract_score(m: dict):
    """Extract best available score from a match object. Returns (h, a) or (None, None)."""
    s = m.get("score", {})
    status = m.get("status", "")
    ft = s.get("fullTime", {})
    h, a = ft.get("home"), ft.get("away")

    if h is None or a is None:
        if status in ("IN_PLAY", "PAUSED"):
            ht = s.get("halfTime", {})
            h, a = ht.get("home"), ht.get("away")
        elif status == "FINISHED":
            rt = s.get("regularTime", {})
            h, a = rt.get("home"), rt.get("away")
            if h is None or a is None:
                ht = s.get("halfTime", {})
                h, a = ht.get("home"), ht.get("away")
    return h, a


def fetch_all_matches():
    try:
        data = api_get(f"/competitions/{COMPETITION_CODE}/matches")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"ERROR: matches endpoint failed: {e}", file=sys.stderr)
        sys.exit(2)
    return data.get("matches", [])


def build_group_scores(matches):
    """Map group stage matches to our fixture IDs."""
    scores = {}
    unmatched = []
    for m in matches:
        stage = m.get("stage", "")
        if stage not in GROUP_STAGE_KEYS and stage != "":
            # Skip non-group matches — they go into knockout
            if stage in KNOCKOUT_STAGES:
                continue
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        utc = m.get("utcDate", "")
        status = m.get("status", "")
        s = m.get("score", {})

        if status not in ("SCHEDULED", "TIMED"):
            ft_d = s.get("fullTime", {})
            rt_d = s.get("regularTime", {})
            ht_d = s.get("halfTime", {})
            print(f"  [{status}] {home} vs {away} | fullTime={ft_d} regularTime={rt_d} halfTime={ht_d}", file=sys.stderr)

        fx_id = map_to_fixture_id(home, away, utc)
        if fx_id is None:
            unmatched.append(f"{home} vs {away} ({utc[:10]})")
            continue

        h, a = extract_score(m)
        if h is None or a is None:
            continue
        if status == "FINISHED":
            hh, aa = s.get("fullTime", {}).get("home"), s.get("fullTime", {}).get("away")
            if hh is None:
                print(f"⚠ FINISHED match {home} vs {away} used fallback score", file=sys.stderr)
        if isinstance(h, int) and isinstance(a, int):
            scores[str(fx_id)] = {"h": h, "a": a}

    if unmatched:
        non_knockout = [u for u in unmatched if not u.startswith("None")]
        if non_knockout:
            print(f"⚠ {len(non_knockout)} group matches could not be mapped:", file=sys.stderr)
            for u in non_knockout[:10]:
                print(f"  - {u}", file=sys.stderr)
    return scores


def build_knockout(matches):
    """Extract knockout fixtures with known teams. Determine winner/runner-up from final."""
    knockout = []
    winner = None
    runnerup = None

    for m in matches:
        stage = m.get("stage", "")
        if stage not in KNOCKOUT_STAGES:
            continue

        home_name = m["homeTeam"].get("name")
        away_name = m["awayTeam"].get("name")
        # Skip placeholders where teams aren't determined yet
        if not home_name or not away_name:
            continue

        home = normalize(home_name)
        away = normalize(away_name)
        status = m.get("status", "")
        utc = m.get("utcDate", "")

        entry = {
            "apiId": m.get("id"),
            "stage": KNOCKOUT_STAGES[stage],
            "stageKey": stage,
            "kickoffUTC": utc,
            "home": home,
            "away": away,
            "status": status,
        }

        h, a = extract_score(m)
        if isinstance(h, int) and isinstance(a, int):
            entry["score"] = {"h": h, "a": a}

            # Determine winner/runner-up from the final
            if stage == "FINAL" and status == "FINISHED":
                s = m.get("score", {})
                penalties = s.get("penalties", {})
                ph = penalties.get("home") if penalties else None
                pa = penalties.get("away") if penalties else None
                if isinstance(ph, int) and isinstance(pa, int):
                    # Decided by penalties
                    if ph > pa:
                        winner, runnerup = home, away
                    else:
                        winner, runnerup = away, home
                elif h > a:
                    winner, runnerup = home, away
                elif a > h:
                    winner, runnerup = away, home

        knockout.append(entry)

    # Sort by kickoff time
    knockout.sort(key=lambda x: x.get("kickoffUTC", ""))
    print(f"  Knockout fixtures with known teams: {len(knockout)}")
    if winner:
        print(f"  🏆 Winner: {winner}, Runner-up: {runnerup}")
    return knockout, winner, runnerup


def fetch_scorers() -> list:
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
    out.sort(key=lambda p: (-p["goals"], p["name"]))
    return out


def main() -> int:
    print(f"Fetching from {API_BASE}/competitions/{COMPETITION_CODE} …")
    matches = fetch_all_matches()
    print(f"  Got {len(matches)} total matches from API.")

    scores = build_group_scores(matches)
    knockout, winner, runnerup = build_knockout(matches)
    scorers = fetch_scorers()

    (DATA_DIR / "scores.json").write_text(
        json.dumps(scores, indent=2, sort_keys=True) + "\n"
    )
    (DATA_DIR / "top-scorers.json").write_text(
        json.dumps(scorers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (DATA_DIR / "knockout.json").write_text(
        json.dumps({
            "fixtures": knockout,
            "winner": winner,
            "runnerup": runnerup,
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (DATA_DIR / "last-updated.json").write_text(
        json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "matchesFound": len(scores),
            "knockoutFound": len(knockout),
            "scorersFound": len(scorers),
        }, indent=2) + "\n"
    )
    print(f"✓ Wrote {len(scores)} group scores, {len(knockout)} knockout fixtures, {len(scorers)} scorers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
