# Telegram Picks Listener — Setup Guide

## What this does
Every 20 minutes, a GitHub Actions job logs into your Telegram account (read-only),
checks your channels for new messages, sends each new message (and any attached
image) to Claude to extract the bet details, and appends a row to your Google
Sheet. It remembers the last message it saw per channel inside the sheet itself,
so nothing needs to stay "always on."

## One-time setup

### 1. Telegram API credentials
- Go to https://my.telegram.org/apps, log in with your phone number, create an app (any name/platform is fine).
- Copy the `api_id` and `api_hash` shown.

### 2. Generate a Telegram session string
Do this on your own computer — not in GitHub Actions, since it needs to text you a login code.
```
pip install telethon
python generate_session.py
```
Enter your api_id/api_hash, then your phone number and the code Telegram sends you.
Copy the printed string — this becomes your `TELEGRAM_SESSION` secret.

Make sure your Telegram account has already joined both channels.

### 3. Edit the channel list
Open `telegram_listener.py` and edit `CHANNELS` near the top with each channel's
username (no `@`). For a private channel with no public username, see the note
at the bottom of this guide.

### 4. Create the Google Sheet
Create a new Google Sheet. Grab the ID from its URL:
`docs.google.com/spreadsheets/d/THIS_PART/edit`

### 5. Create a Google service account
- Go to https://console.cloud.google.com → create a project (free).
- Enable the **Google Sheets API** and **Google Drive API**.
- IAM & Admin → Service Accounts → Create → then open it → Keys → Add Key → JSON. Download it.
- Open the JSON, find `client_email`, and share your Google Sheet with that address as an Editor.

### 6. Get an Anthropic API key
Create one at https://console.anthropic.com (separate from a claude.ai subscription —
pay-as-you-go; parsing a handful of messages a day costs a few cents a month).

### 7. Push to a GitHub repo
Create a new **private** repo and push all these files, keeping the
`.github/workflows/` folder structure intact.

### 8. Add GitHub secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_SESSION`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the entire downloaded JSON file content)
- `GOOGLE_SHEET_ID`
- `ANTHROPIC_API_KEY`

### 9. Run it
Actions tab → "Telegram Picks Listener" → Run workflow, to trigger it manually
the first time and confirm rows land in your sheet. After that it runs on its
own every 20 minutes.

## Good to know
- GitHub auto-pauses scheduled workflows after 60 days with no commits to the repo.
  If it silently stops, push any small commit or re-trigger it manually.
- The first run on each channel backfills the last 15 messages; after that it
  only processes new ones.
- For a private channel with no public username, run this once locally to find
  its numeric ID:
```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
with TelegramClient(StringSession("YOUR_SESSION_STRING"), API_ID, "API_HASH") as c:
    for d in c.iter_dialogs():
        print(d.id, d.name)
```
Use the printed numeric ID in place of a username string in `CHANNELS`.
