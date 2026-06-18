"""
Picks Grader
------------
Reads open picks from the Picks tab, fetches game scores from ESPN's
free API, and uses Claude to determine W/L/Push for completed games.
Runs on its own GitHub Actions schedule (every 2 hours).
"""

import os
import re
import json
from datetime import datetime, timedelta, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials
import anthropic

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
PICKS_TAB = "Picks"
RESULT_COL = 13   # 1-based index of the "result" column

# Maps sport strings Claude might extract → ESPN (sport, league) path
SPORT_ESPN_MAP = [
    (["mlb", "baseball"],                     ("baseball",    "mlb")),
    (["nba", "basketball"],                   ("basketball",  "nba")),
    (["wnba"],                                ("basketball",  "wnba")),
    (["nfl", "nfl football"],                 ("football",    "nfl")),
    (["nhl", "hockey"],                       ("hockey",      "nhl")),
    (["ncaaf", "college football"],           ("football",    "college-football")),
    (["ncaab", "college basketball"],         ("basketball",  "mens-college-basketball")),
    (["mls", "major league soccer"],          ("soccer",      "usa.1")),
    (["world cup", "fifa"],                   ("soccer",      "fifa.world")),
    (["ufl"],                                 ("football",    "ufl")),
    (["tennis", "atp", "wta"],               ("tennis",      "atp")),
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def get_sheet():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])


def normalize_sport(sport_str):
    if not sport_str:
        return None
    s = sport_str.lower().strip()
    # Remove "wnba" before checking "nba"
    for keywords, espn_path in SPORT_ESPN_MAP:
        if any(k in s for k in keywords):
            return espn_path
    return None


def fetch_espn_scores(sport, league, date_str):
    """
    date_str: YYYYMMDD
    Returns list of dicts: {name, home_team, away_team, home_score,
                            away_score, completed, status}
    """
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
    try:
        resp = requests.get(url, params={"dates": date_str}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    ESPN fetch error ({sport}/{league} {date_str}): {e}")
        return []

    games = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        status = event.get("status", {}).get("type", {})
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        games.append({
            "name": event.get("name", ""),
            "home_team": home.get("team", {}).get("displayName", ""),
            "away_team": away.get("team", {}).get("displayName", ""),
            "home_score": home.get("score", "?"),
            "away_score": away.get("score", "?"),
            "completed": status.get("completed", False),
            "status": status.get("description", "Unknown"),
        })
    return games


def grade_with_claude(claude_client, pick, games_text):
    prompt = f"""You are grading a sports bet. Available game scores for this sport and date:

{games_text}

Bet details:
- Capper: {pick.get("capper")}
- Matchup: {pick.get("matchup")}
- Bet type: {pick.get("bet_type")}
- Selection: {pick.get("selection")}
- Odds: {pick.get("odds")}

Instructions:
1. Find the matching game from the scores above.
2. If no matching game is found, respond: {{"result": "no_match"}}
3. If the game is not yet final, respond: {{"result": "pending"}}
4. If the game is final, determine the outcome and respond with one of:
   {{"result": "W"}} {{"result": "L"}} {{"result": "Push"}}

Respond with ONLY the JSON object, no explanation."""

    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw).get("result", "no_match")
    except Exception as e:
        print(f"    Claude grading error: {e}")
        return "error"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    sheet = get_sheet()
    ws = sheet.worksheet(PICKS_TAB)

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        print("No picks found.")
        return

    headers = all_values[0]
    open_picks = []
    for i, row in enumerate(all_values[1:], start=2):
        # Pad short rows
        while len(row) < RESULT_COL:
            row.append("")
        if row[RESULT_COL - 1].strip():   # result already filled
            continue
        pick = dict(zip(headers, row))
        pick["_row"] = i
        open_picks.append(pick)

    print(f"Found {len(open_picks)} open pick(s) to grade")

    # Group by (sport, date) to minimize ESPN API calls
    groups = {}
    for pick in open_picks:
        sport_raw = pick.get("sport", "") or ""
        date_only = (pick.get("date_utc") or "")[:10]  # YYYY-MM-DD
        espn = normalize_sport(sport_raw)
        key = (espn, date_only)
        groups.setdefault(key, []).append(pick)

    for (espn, date_only), picks in groups.items():
        print(f"\n--- {espn} | {date_only} ({len(picks)} pick(s)) ---")

        if not espn or not date_only:
            for pick in picks:
                print(f"  Row {pick['_row']}: sport/date unknown → skipping")
            continue

        # Fetch scores (also try day before/after for late night games)
        date_fmt = date_only.replace("-", "")
        all_games = []
        for offset in [0, -1, 1]:
            d = (datetime.strptime(date_only, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y%m%d")
            all_games.extend(fetch_espn_scores(espn[0], espn[1], d))

        if not all_games:
            games_text = "No games found."
        else:
            lines = []
            for g in all_games:
                lines.append(
                    f"- {g['away_team']} @ {g['home_team']}: "
                    f"{g['away_score']}-{g['home_score']} ({g['status']})"
                )
            games_text = "\n".join(lines)

        print(f"  ESPN returned {len(all_games)} game(s)")

        for pick in picks:
            result = grade_with_claude(claude_client, pick, games_text)
            print(f"  Row {pick['_row']} | {pick.get('capper')} | {pick.get('selection')} → {result}")

            if result in ("W", "L", "Push"):
                ws.update_cell(pick["_row"], RESULT_COL, result)
            elif result == "pending":
                pass   # leave blank, try again next run
            elif result == "no_match":
                ws.update_cell(pick["_row"], RESULT_COL, "no_match")

    print("\nGrading complete.")


if __name__ == "__main__":
    main()
