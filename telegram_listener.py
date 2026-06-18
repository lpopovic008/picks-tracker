"""
Telegram Picks Listener
------------------------
Polls Telegram channels for new messages, extracts bet info via Claude Haiku,
and writes rows to Google Sheets. Runs on GitHub Actions cron schedule.
"""

import os
import re
import json
import base64
from io import BytesIO

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

import gspread
from google.oauth2.service_account import Credentials

import anthropic

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

CHANNELS = [
    "betting_intel",           # public: username without @, private: -1001234567890
    -1003984449468,            # CapperSync
    -1003641992899,            # MONEYCAPPERSFREE
    -1003641018140,            # EXCLUSIVE PLAYS
    -1002077943194,            # CAPPERS FREE 🎰
    #-1001858676502,            # Life’s a Gamble 🎲
]

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
FIRST_RUN_BACKFILL = 15
PICKS_TAB = "Picks"
STATE_TAB = "_state"

EXTRACTION_PROMPT = """You are reading a message from a Telegram sports betting channel.
Channels post in different styles: sometimes plain text where the capper's name
appears as a leading hashtag (e.g. "#JohnnyBets Lakers -4.5 -110 2u"), and
sometimes an image of a bet slip with a short text caption that names the capper.
Figure out which style applies to this particular message and extract accordingly.

Identify every distinct bet mentioned. Respond with ONLY a JSON array (no
markdown fences, no explanation) where each item has exactly this shape:

[
  {
    "capper": string or null,
    "sport": string or null,
    "matchup": string or null,
    "bet_type": "spread" | "total" | "moneyline" | "prop" | "parlay" | "other" | null,
    "selection": string or null,
    "odds": string or null,
    "units_or_confidence": string or null,
    "notes": string or null
  }
]

If you cannot find any bet information at all, respond with []."""

# ----------------------------------------------------------------------
# Google Sheets helpers
# ----------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheet():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])


def get_or_create_worksheet(sheet, title, header):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=1000, cols=len(header) + 2)
        ws.append_row(header)
        return ws


def load_state(ws):
    rows = ws.get_all_records()
    return {r["channel"]: int(r["last_message_id"]) for r in rows if r.get("channel")}


def save_state(ws, channel, message_id):
    values = ws.get_all_values()
    for i, row in enumerate(values):
        if row and row[0] == str(channel):
            ws.update_cell(i + 1, 2, message_id)
            return
    ws.append_row([str(channel), message_id])


# ----------------------------------------------------------------------
# Claude extraction
# ----------------------------------------------------------------------

def extract_bets(claude_client, text, image_bytes, msg_id):
    content = []

    if image_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            },
        })
        print(f"  [msg {msg_id}] image ({len(image_bytes)} bytes) + text")
    else:
        print(f"  [msg {msg_id}] text only")

    print(f"  [msg {msg_id}] preview: {repr((text or '')[:120])}")

    content.append({
        "type": "text",
        "text": f"Message text/caption:\n{text or '(none)'}"
    })

    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            system=EXTRACTION_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        print(f"  [msg {msg_id}] Claude: {raw[:200]}")
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        bets = json.loads(raw)
        result = bets if isinstance(bets, list) else []
        print(f"  [msg {msg_id}] {len(result)} bet(s) extracted")
        return result

    except Exception as e:
        print(f"  [msg {msg_id}] ERROR: {str(e)[:200]}")
        return []


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    tg_client = TelegramClient(
        StringSession(os.environ["TELEGRAM_SESSION"]),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )

    sheet = get_sheet()
    state_ws = get_or_create_worksheet(sheet, STATE_TAB, ["channel", "last_message_id"])
    picks_ws = get_or_create_worksheet(sheet, PICKS_TAB, [
        "date_utc", "channel", "message_id", "capper", "sport", "matchup",
        "bet_type", "selection", "odds", "units_or_confidence", "notes",
        "message_link", "result",
    ])
    state = load_state(state_ws)

    with tg_client:
        for channel in CHANNELS:
            print(f"\n=== Channel: {channel} ===")
            last_id = state.get(str(channel), 0)
            print(f"  Last ID: {last_id}")

            try:
                if last_id:
                    messages = list(tg_client.iter_messages(channel, min_id=last_id, reverse=True))
                else:
                    messages = list(tg_client.iter_messages(channel, limit=FIRST_RUN_BACKFILL))
                    messages.reverse()
            except Exception as e:
                print(f"  ERROR reading channel: {e}")
                continue

            print(f"  {len(messages)} new message(s) to process")
            if not messages:
                continue

            newest_id = last_id

            for msg in messages:
                newest_id = max(newest_id, msg.id)
                try:
                    text = msg.text or ""
                    image_bytes = None

                    if msg.photo:
                        buf = BytesIO()
                        tg_client.download_media(msg, file=buf)
                        image_bytes = buf.getvalue()
                    elif not text:
                        print(f"  [msg {msg.id}] no text or photo, skipping")
                        continue

                    bets = extract_bets(claude_client, text, image_bytes, msg.id)

                    if bets:
                        link = f"https://t.me/{channel}/{msg.id}"
                        date_str = msg.date.strftime("%Y-%m-%d %H:%M UTC")
                        for bet in bets:
                            picks_ws.append_row([
                                date_str, str(channel), msg.id,
                                bet.get("capper"), bet.get("sport"), bet.get("matchup"),
                                bet.get("bet_type"), bet.get("selection"), bet.get("odds"),
                                bet.get("units_or_confidence"), bet.get("notes"),
                                link, "",
                            ])
                            print(f"  [msg {msg.id}] ROW: {bet.get('capper')} | {bet.get('matchup')} | {bet.get('selection')}")

                except Exception as e:
                    print(f"  [msg {msg.id}] ERROR: {e}")

            save_state(state_ws, str(channel), newest_id)
            print(f"  State saved at ID {newest_id}")


if __name__ == "__main__":
    main()
