"""
Run this ONCE on your own computer — never inside GitHub Actions, since it
needs to receive an interactive login code from Telegram.

1. pip install telethon
2. Get your api_id and api_hash from https://my.telegram.org/apps
3. Run: python generate_session.py
4. Enter your phone number and the login code Telegram texts/sends you
5. Copy the printed string into a GitHub secret named TELEGRAM_SESSION
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = input("api_id: ").strip()
api_hash = input("api_hash: ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    session_string = client.session.save()
    print("\nSession string (copy this whole line into the TELEGRAM_SESSION secret):\n")
    print(session_string)
    print("\nKeep this private — it's equivalent to your Telegram login.")
