"""
Picks Grader
------------
Reads open picks from the Picks tab, uses Claude + web search to find
the final score for any sport/league worldwide, and grades each bet W/L/Push.
Runs on its own GitHub Actions schedule (every 2 hours).
"""

import os
import re
import json
import time

import gspread
from google.oauth2.service_account import Credentials
import anthropic

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

CLAUDE_MODEL  = "claude-haiku-4-5-20251001"
PICKS_TAB     = "Picks"
RESULT_COL    = 13    # 1-based column index of "result"
DELAY_BETWEEN = 2     # seconds between Claude calls

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ----------------------------------------------------------------------
# Sheets helpers
# ----------------------------------------------------------------------

def get_sheet():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])


# ----------------------------------------------------------------------
# Grading via Claude + web search
# ----------------------------------------------------------------------

def grade_pick(claude_client, pick):
    """
    Asks Claude to search the web for the game result and grade the bet.
    Returns: "W" | "L" | "Push" | "pending" | "no_match" | "error"
    """
    date_str  = str(pick.get("date_utc", ""))[:10]
    matchup   = pick.get("matchup", "")
    sport     = pick.get("sport", "")
    bet_type  = pick.get("bet_type", "")
    selection = pick.get("selection", "")
    odds      = pick.get("odds", "")

    prompt = f"""Search for the final score of this game and grade the bet.

Game:       {matchup}
Sport:      {sport}
Date:       {date_str}
Bet type:   {bet_type}
Selection:  {selection}
Odds:       {odds}

Instructions:
1. Search for the final score of this exact game on this date.
2. If the game has not been played yet or is still in progress → {{"result": "pending"}}
3. If you cannot find the game after searching → {{"result": "no_match"}}
4. If the game is final, determine whether the bet won, lost, or pushed.
   - For spreads: check if the selected team covered the spread.
   - For totals: check if the combined score went over or under.
   - For moneylines: check if the selected team won.
   - For props: check if the prop hit.

Respond ONLY with a JSON object — no explanation, no markdown:
{{"result": "W"}} or {{"result": "L"}} or {{"result": "Push"}} or {{"result": "pending"}} or {{"result": "no_match"}}"""

    messages = [{"role": "user", "content": prompt}]

    try:
        for _ in range(6):   # allow up to 6 turns for web search loop
            resp = claude_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=300,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

            if resp.stop_reason == "end_turn":
                raw = "".join(
                    b.text for b in resp.content if b.type == "text"
                ).strip()
                raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
                try:
                    return json.loads(raw).get("result", "no_match")
                except Exception:
                    return "no_match"

            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = [
                    {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                    for b in resp.content if b.type == "tool_use"
                ]
                messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        print(f"    Claude error: {str(e)[:200]}")
        return "error"

    return "no_match"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    sheet = get_sheet()
    ws    = sheet.worksheet(PICKS_TAB)

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        print("No picks found.")
        return

    headers = all_values[0]

    # Collect rows that need grading
    open_picks = []
    for i, row in enumerate(all_values[1:], start=2):
        while len(row) < RESULT_COL:
            row.append("")
        result_val = row[RESULT_COL - 1].strip()
        if result_val in ("W", "L", "Push"):   # final — skip
            continue
        # blank / no_match / error / pending → retry
        pick = dict(zip(headers, row))
        pick["_row"] = i
        open_picks.append(pick)

    print(f"Found {len(open_picks)} pick(s) to grade")

    for pick in open_picks:
        label = f"{pick.get('capper')} | {pick.get('matchup')} | {pick.get('selection')}"
        print(f"\nGrading row {pick['_row']}: {label}")

        result = grade_pick(claude_client, pick)
        print(f"  → {result}")

        if result in ("W", "L", "Push"):
            ws.update_cell(pick["_row"], RESULT_COL, result)
        elif result == "no_match":
            ws.update_cell(pick["_row"], RESULT_COL, "no_match")
        elif result == "pending":
            pass   # leave blank, retry next run
        elif result == "error":
            pass   # leave blank, retry next run

        time.sleep(DELAY_BETWEEN)

    print("\nGrading complete.")


if __name__ == "__main__":
    main()
