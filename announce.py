"""Post a one-off official announcement to the Telegram channel.

Run manually from GitHub: Actions -> "Post Official Announcement" -> Run workflow.
Required secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import sys
from html import escape

import requests

MAX_TEXT = 800


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    text = os.environ.get("ANNOUNCE_TEXT", "").strip()
    link = os.environ.get("ANNOUNCE_LINK", "").strip()

    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
        return 1
    if not text:
        print("No announcement text was given.")
        return 1
    if len(text) > MAX_TEXT:
        print(f"Announcement is {len(text)} characters; the limit is {MAX_TEXT}.")
        return 1
    if link and (not link.startswith("https://") or any(c in link for c in ' "<>')):
        print("The link must start with https:// and contain no spaces or quotes.")
        return 1

    message = "📢 <b>Official update</b>\n" + escape(text)
    if link:
        message += f'\n\n<a href="{escape(link, quote=True)}">Read the full announcement</a>'

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}},
            timeout=30,
        )
        ok = response.status_code == 200 and response.json().get("ok")
    except (requests.RequestException, ValueError):
        # Never print the exception: it can contain the bot token.
        print("Could not reach Telegram; delivery is unconfirmed.")
        return 1
    if not ok:
        print("Telegram rejected the announcement. Check the bot's permissions and secrets.")
        return 1
    print("Announcement sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
