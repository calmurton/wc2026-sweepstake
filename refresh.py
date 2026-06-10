#!/usr/bin/env python3
"""
refresh.py — fetches live World Cup 2026 scores from football-data.org
and patches the DATA object in sweepstake_dashboard.html.

Requires: requests
  pip install requests
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("FOOTBALL_API_KEY")
if not API_KEY:
    print("ERROR: FOOTBALL_API_KEY environment variable not set.")
    sys.exit(1)

HTML_FILE = "index.html"

# football-data.org competition ID for FIFA World Cup 2026
# WC = 2000 on football-data.org (same ID used for every World Cup)
COMPETITION_ID = 2000

HEADERS = {"X-Auth-Token": API_KEY}
BASE_URL = "https://api.football-data.org/v4"


def fetch(path):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code == 429:
        print("Rate limited by API — skipping this run.")
        sys.exit(0)
    r.raise_for_status()
    return r.json()


def extract_scores(matches):
    """
    Returns a dict of { fixture_id_in_html: {h, a} } for finished/in-play matches.
    football-data.org match IDs won't match our internal IDs, so we match by
    home team name + away team name instead.
    """
    scores = {}
    for m in matches:
        status = m.get("status", "")
        if status not in ("FINISHED", "IN_PLAY", "PAUSED"):
            continue
        score = m.get("score", {})
        ft = score.get("fullTime", {})
        # Fall back to current score if match is live
        if ft.get("home") is None:
            ft = score.get("regularTime", {})
        if ft.get("home") is None:
            continue
        home_team = m["homeTeam"]["name"]
        away_team = m["awayTeam"]["name"]
        scores[(home_team, away_team)] = {
            "h": ft["home"],
            "a": ft["away"],
        }
    return scores


def normalise_name(api_name):
    """
    football-data.org uses slightly different country names in places.
    Map them to whatever is in DATA.fixtures in the HTML.
    Extend this dict if you spot mismatches.
    """
    mapping = {
        "USA": "USA",
        "United States": "USA",
        "Korea Republic": "South Korea",
        "IR Iran": "Iran",
        "Czechia": "Czech Republic",
        "Bosnia-Herzegovina": "Bosnia and Herzegovina",
        "DR Congo": "DR Congo",
        "Congo DR": "DR Congo",
        "Cote d'Ivoire": "Ivory Coast",
        "Côte d'Ivoire": "Ivory Coast",
        "Curacao": "Curaçao",
        "Curaçao": "Curaçao",
        "Cape Verde Islands": "Cape Verde",
    }
    return mapping.get(api_name, api_name)


def extract_scorers(scorers_data):
    """Returns a sorted list of top scorers."""
    out = []
    for s in scorers_data.get("scorers", []):
        player = s.get("player", {})
        team = s.get("team", {})
        goals = s.get("goals", 0) or 0
        if goals == 0:
            continue
        out.append({
            "name": player.get("name", "Unknown"),
            "team": normalise_name(team.get("name", "")),
            "goals": goals,
        })
    out.sort(key=lambda x: -x["goals"])
    return out


def build_embedded_scores(api_scores, html_fixtures):
    """
    Match API results (keyed by team name pair) to our fixture IDs.
    Returns { fixture_id: {h, a} }
    """
    embedded = {}
    for fx in html_fixtures:
        home = fx["home"]
        away = fx["away"]
        # Try direct match first
        result = api_scores.get((home, away))
        if result is None:
            # Try normalised names from the API side
            for (ah, aa), score in api_scores.items():
                if normalise_name(ah) == home and normalise_name(aa) == away:
                    result = score
                    break
        if result is not None:
            embedded[str(fx["id"])] = result
    return embedded


def patch_html(html_path, new_scores, new_scorers, refreshed_iso):
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the existing DATA JSON blob
    pattern = r'(const DATA = )(\{.*?\});'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print("ERROR: Could not find DATA object in HTML.")
        sys.exit(1)

    data = json.loads(match.group(2))

    # Patch only the fields we own — leave entrants, groups, fixtures, flags, prizes untouched
    data["embeddedScores"] = new_scores
    data["topScorers"] = new_scorers
    data["lastRefreshed"] = refreshed_iso
    

    new_data_str = "const DATA = " + json.dumps(data, ensure_ascii=False, separators=(',', ':')) + ";"
    new_content = content[:match.start()] + new_data_str + content[match.end():]

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Patched {html_path}: {len(new_scores)} scores, {len(new_scorers)} scorers.")


def main():
    print("Fetching matches...")
    matches_data = fetch(f"/competitions/{COMPETITION_ID}/matches")
    matches = matches_data.get("matches", [])
    print(f"  Got {len(matches)} matches from API.")

    print("Fetching top scorers...")
    try:
        scorers_data = fetch(f"/competitions/{COMPETITION_ID}/scorers?limit=20")
        scorers = extract_scorers(scorers_data)
    except Exception as e:
        print(f"  Could not fetch scorers ({e}), skipping.")
        scorers = []

    api_scores = extract_scores(matches)
    print(f"  {len(api_scores)} finished/live matches found.")

    # Load fixture list from the HTML so we can match IDs
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = r'const DATA = (\{.*?\});'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print("ERROR: Could not find DATA in HTML.")
        sys.exit(1)
    data = json.loads(match.group(1))
    html_fixtures = data.get("fixtures", [])

    embedded_scores = build_embedded_scores(api_scores, html_fixtures)
    embedded_scores["999"] = {"h": 1, "a": 0}  # test entry
    refreshed_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    patch_html(HTML_FILE, embedded_scores, scorers, refreshed_iso)


if __name__ == "__main__":
    main()
