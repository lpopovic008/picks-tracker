"""
Telegram Picks Listener
------------------------
Polls Telegram channels for new messages, extracts bet info via Claude Haiku,
filters out recaps/promos, deduplicates across channels, and writes to Google Sheets.
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
    -1003641018140,            # CAPPERS FREE 🎰
    -1001858676502,            # Life’s a Gamble 🎲
]

# ------------------------------------------------------------------
# Per-channel context — fill this in for each channel you track.
# This tells Claude WHERE to find the capper name in each channel's
# posts so it doesn't confuse the channel name with the capper name.
#
# Key   = channel username string OR numeric ID (must match CHANNELS exactly)
# Value = plain-English description of the channel's posting format
# ------------------------------------------------------------------
CHANNEL_CONTEXT = {
    "betting-intel": (
        "Aggregator channel. Each post covers a different capper. "
        "The capper name appears at the very start of the message text or image caption or in the image itself."
        "before the pick details. Do NOT use the channel name as the capper."
    ),
    -1003984449468: (
        "Aggregator channel. Capper names appear as hashtags like #HammeringHank "
        "at the start of each post. Extract the word after # as the capper name. "
        "Do NOT use the channel name as the capper."
    ),
    -1003641018140: (
        "Aggregator channel. Capper names appear as comments to the post"
        "at the start of each post. Extract the comment as the name. "
        "Do NOT use the channel name as the capper."
    )
}

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
FIRST_RUN_BACKFILL = 15
PICKS_TAB = "Picks"
STATE_TAB = "_state"


def build_prompt(channel, channel_context):
    return f"""You are reading a message from a Telegram sports betting channel.

Channel: {channel}
Channel format: {channel_context}

Use the channel format description above to correctly identify the capper name.
The capper name ALWAYS comes from the message content, never from the channel name itself.

For each message, classify it and extract any bets found.
Respond with ONLY a JSON array (no markdown fences, no explanation).
Each item must have exactly this shape:

[
  {{
    "pick_status": "new_pick" | "result" | "recap" | "promo",
    "capper": string or null,
    "sport": string or null,
    "matchup": string or null,
    "bet_type": "spread" | "total" | "moneyline" | "prop" | "parlay" | "other" | null,
    "selection": string or null,
    "odds": string or null,
    "units_or_confidence": string or null,
    "notes": string or null
  }}
]

pick_status rules:
- "new_pick": a fresh bet posted for the first time; the game has not yet started
- "result": reporting the outcome of a past bet (✅ ❌, "won", "lost", "hit", W/L records)
- "recap": summarising recent performance across multiple picks
- "promo": advertisement, invite link, or call to join a VIP/premium channel

If no bet information at all, respond with [].
If a message mixes new picks and results, return a separate object for each."""


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


def load_existing_picks(ws):
    seen_messages = set()
    seen_picks = set()
    rows = ws.get_all_records()
    for r in rows:
        ch = str(r.get("channel", "")).strip()
        mid = str(r.get("message_id", "")).strip()
        if ch and mid:
            seen_messages.add((ch, mid))
        capper = str(r.get("capper", "")).lower().strip()
        selection = str(r.get("selection", "")).lower().strip()
        date_only = str(r.get("date_utc", ""))[:10]
        if capper and selection and date_only:
            seen_picks.add((capper, date_only, selection))
    return seen_messages, seen_picks


# ----------------------------------------------------------------------
# Claude extraction
# ----------------------------------------------------------------------

def extract_bets(claude_client, text, image_bytes, msg_id, channel):
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

    channel_context = CHANNEL_CONTEXT.get(
        channel,
        "Format unknown. Extract the capper name from the message content. "
        "Do not use the channel name as the capper name."
    )
    prompt = build_prompt(channel, channel_context)

    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            system=prompt,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        bets = json.loads(raw)
        result = bets if isinstance(bets, list) else []
        new_picks = [b for b in result if b.get("pick_status") == "new_pick"]
        skipped = len(result) - len(new_picks)
        print(f"  [msg {msg_id}] {len(new_picks)} new pick(s), {skipped} skipped (recap/result/promo)")
        return new_picks

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
    seen_messages, seen_picks = load_existing_picks(picks_ws)
    print(f"Loaded {len(seen_messages)} existing message IDs, {len(seen_picks)} existing picks for dedup")

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

                msg_key = (str(channel), str(msg.id))
                if msg_key in seen_messages:
                    print(f"  [msg {msg.id}] already processed, skipping")
                    continue

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

                    bets = extract_bets(claude_client, text, image_bytes, msg.id, channel)

                    for bet in bets:
                        date_str = msg.date.strftime("%Y-%m-%d %H:%M UTC")
                        date_only = date_str[:10]
                        capper_key = str(bet.get("capper") or "").lower().strip()
                        selection_key = str(bet.get("selection") or "").lower().strip()
                        pick_key = (capper_key, date_only, selection_key)

                        if pick_key in seen_picks and capper_key and selection_key:
                            print(f"  [msg {msg.id}] DUPE skipped: {bet.get('capper')} | {bet.get('selection')}")
                            continue

                        link = f"https://t.me/{channel}/{msg.id}"
                        picks_ws.append_row([
                            date_str, str(channel), msg.id,
                            bet.get("capper"), bet.get("sport"), bet.get("matchup"),
                            bet.get("bet_type"), bet.get("selection"), bet.get("odds"),
                            bet.get("units_or_confidence"), bet.get("notes"),
                            link, "",
                        ])
                        seen_picks.add(pick_key)
                        seen_messages.add(msg_key)
                        print(f"  [msg {msg.id}] ROW: {bet.get('capper')} | {bet.get('matchup')} | {bet.get('selection')}")

                except Exception as e:
                    print(f"  [msg {msg.id}] ERROR: {e}")

            save_state(state_ws, str(channel), newest_id)
            print(f"  State saved at ID {newest_id}")


if __name__ == "__main__":
    main()
