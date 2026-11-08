"""
GroupMe Trigger Bot
--------------------
Polls a GroupMe group for new messages and runs a Python action (and/or
posts a reply through your bot) whenever a specific trigger phrase appears.

WHY POLLING INSTEAD OF A WEBHOOK?
GroupMe bots normally receive messages via a "callback URL" that GroupMe
pushes messages to. That requires a public URL (a server), which isn't
practical for "just run this on my computer." Polling the Groups API on
an interval works fine for a local script and needs no public endpoint.

WHAT YOU NEED TO FILL IN BELOW:
1. ACCESS_TOKEN  - your GroupMe access token (from https://dev.groupme.com,
                    click "Access Token" in the top right after logging in)
2. GROUP_ID      - the ID of the group you want to watch (see find_group_id()
                    helper below, or check the group's info in dev.groupme.com)
3. BOT_ID        - your bot's bot_id (from https://dev.groupme.com/bots),
                    only needed if you want the script to POST messages back
                    through the bot
4. TRIGGER_PHRASE - the text to watch for (case-insensitive substring match
                    by default)
5. on_trigger()  - put whatever Python you want to run when the phrase is seen

Install dependency:
    pip install requests
"""

import time
import requests

# ------------------- CONFIG (edit these) -------------------
ACCESS_TOKEN = "0ad489508e06013f3bb9126fa44dee51"     # personal access token
GROUP_ID = "117341567"             # group to watch
BOT_ID = "c98ce4a56508d8a788abad53e8"                 # bot used to post replies (optional)
TRIGGER_PHRASE = "sleep"           # phrase to watch for (lowercase)
POLL_INTERVAL_SECONDS = 5              # how often to check for new messages
REPLY_ON_TRIGGER = "Trigger received!" # set to None to skip auto-reply
# -------------------------------------------------------------

GROUPME_API = "https://api.groupme.com/v3"


def find_group_id():
    """Helper: prints your groups and their IDs so you can fill in GROUP_ID."""
    resp = requests.get(
        f"{GROUPME_API}/groups",
        params={"token": ACCESS_TOKEN, "per_page": 100},
    )
    resp.raise_for_status()
    for group in resp.json().get("response", []):
        print(f"{group['name']!r:40} id={group['id']}")


def get_latest_messages(after_id=None, limit=20):
    """Fetch messages newer than after_id.

    NOTE: GroupMe's own docs flag `since_id` as unreliable for this and
    recommend `after_id` instead, so that's what we use here.
    """
    params = {"token": ACCESS_TOKEN, "limit": limit}
    if after_id:
        params["after_id"] = after_id

    resp = requests.get(
        f"{GROUPME_API}/groups/{GROUP_ID}/messages", params=params
    )
    if resp.status_code == 304:
        # No new messages
        return []
    resp.raise_for_status()
    messages = resp.json()["response"]["messages"]
    # API returns newest-first; flip to chronological order
    return list(reversed(messages))


def send_bot_message(text):
    """Post a message to the group through your bot."""
    if not BOT_ID or BOT_ID == "YOUR_BOT_ID":
        print("[warn] BOT_ID not set, skipping reply.")
        return
    resp = requests.post(
        f"{GROUPME_API}/bots/post",
        json={"bot_id": BOT_ID, "text": text},
    )
    resp.raise_for_status()


def on_trigger(message):
    print('sup chumps')

    sender = message.get("name", "someone")
    text = message.get("text", "")
    print(f"[TRIGGER] {sender} said: {text}")

    # Example action: reply through the bot
    if REPLY_ON_TRIGGER:
        send_bot_message(REPLY_ON_TRIGGER)

    # Example action: run any other python you want here, e.g.:
    # subprocess.run(["say", "trigger detected"])   # macOS text-to-speech
    # requests.post("http://localhost:5000/webhook", json={"text": text})


def main():
    print("Starting GroupMe trigger bot...")
    print(f"Watching group {GROUP_ID} for phrase: {TRIGGER_PHRASE!r}")

    last_seen_id = None

    # Prime last_seen_id with the most recent message so we don't
    # re-trigger on old messages when the script starts.
    initial = get_latest_messages(limit=1)
    if initial:
        last_seen_id = initial[-1]["id"]

    while True:
        try:
            messages = get_latest_messages(after_id=last_seen_id)
            for message in messages:
                last_seen_id = message["id"]
                text = (message.get("text") or "").lower()
                print(f"[debug] saw message: {text!r}")
                if TRIGGER_PHRASE.lower() in text:
                    on_trigger(message)
        except requests.RequestException as e:
            print(f"[error] request failed: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    # Uncomment this once to list your groups and find GROUP_ID:
    # find_group_id()

    main()
