"""
Alert-only Telegram notifier: when any account in handles.txt posts a new tweet,
send a Telegram message with the text and a link. It never posts or replies on X.

Usage:
    python notifier.py --chat-id   # print your Telegram chat id (send /start to the bot first)
    python notifier.py --once      # check once and exit
    python notifier.py             # keep running, checking every poll_minutes
"""
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
HANDLES_FILE = ROOT / "handles.txt"
STATE_FILE = ROOT / "state.json"

TWITTERAPI_BASE = "https://api.twitterapi.io"
BATCH_SIZE = 20  # handles per search query (one paid request per batch)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def http_get_json(url, headers=None, retries=4):
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # free-tier accounts are rate limited; back off and retry
            if e.code != 429 or attempt == retries:
                raise
            time.sleep(6 * (attempt + 1))


def http_post_json(url, payload, retries=3):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Telegram limits channels to ~20 messages/minute and says how long to wait
            if e.code != 429 or attempt == retries:
                raise
            try:
                wait = json.loads(e.read().decode("utf-8"))["parameters"]["retry_after"]
            except Exception:
                wait = 30
            time.sleep(wait + 1)


def load_handles():
    if not HANDLES_FILE.exists():
        sys.exit("handles.txt not found. Run fetch_handles.py or add usernames manually.")
    handles = []
    for line in HANDLES_FILE.read_text(encoding="utf-8-sig").splitlines():
        h = line.strip().lstrip("@")
        if h and not h.startswith("#"):
            handles.append(h)
    return handles


def save_handles(handles):
    HANDLES_FILE.write_text("\n".join(handles) + "\n", encoding="utf-8")


def parse_handle(arg):
    """Accept 'name', '@name' or an x.com/twitter.com profile link."""
    arg = arg.strip()
    for prefix in ("https://", "http://", "www.", "x.com/", "twitter.com/"):
        if arg.lower().startswith(prefix):
            arg = arg[len(prefix):]
    return arg.split("/")[0].split("?")[0].lstrip("@")


def lookup_user(api_key, handle):
    """Return the account's real username and follower count, or None if it doesn't exist."""
    url = f"{TWITTERAPI_BASE}/twitter/user/info?{urllib.parse.urlencode({'userName': handle})}"
    try:
        data = http_get_json(url, {"X-API-Key": api_key}).get("data") or {}
    except Exception:
        return None
    return (data["userName"], data.get("followers")) if data.get("userName") else None


HELP_TEXT = (
    "<b>Volta Radar commands</b>\n\n"
    "/add username  (or @username or x.com link)\n"
    "/remove username\n"
    "/list  show tracked accounts\n\n"
    "Commands are read every few minutes, so replies can take a short while."
)


def handle_command(cfg, handles, text):
    """Apply one admin command to the handles list; return the reply text."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    lower = [h.lower() for h in handles]

    if cmd == "/list":
        lines = "\n".join(f"{i}. @{html.escape(h)}" for i, h in enumerate(handles, 1))
        return f"<b>Tracking {len(handles)} accounts</b>\n\n{lines}"

    if cmd in ("/add", "/remove"):
        names = [parse_handle(a) for a in arg.replace(",", " ").split() if parse_handle(a)]
        if not names:
            return f"Usage: {cmd} username"
        replies = []
        for name in names:
            if cmd == "/add":
                if name.lower() in lower:
                    replies.append(f"@{html.escape(name)} is already tracked")
                    continue
                found = lookup_user(cfg["twitterapi_key"], name)
                if not found:
                    replies.append(f"❌ @{html.escape(name)} not found on X")
                    continue
                real, followers = found
                handles.append(real)
                lower.append(real.lower())
                f = f" ({followers:,} followers)" if isinstance(followers, int) else ""
                replies.append(f"✅ Added @{html.escape(real)}{f}")
            else:
                if name.lower() not in lower:
                    replies.append(f"@{html.escape(name)} is not in the list")
                    continue
                idx = lower.index(name.lower())
                replies.append(f"🗑 Removed @{html.escape(handles.pop(idx))}")
                lower.pop(idx)
        return "\n".join(replies) + f"\n\nNow tracking {len(handles)} accounts."

    return HELP_TEXT


def process_commands(cfg, state):
    """Read new Telegram messages and apply commands sent by the admin."""
    admin = str(cfg.get("telegram_admin_id") or "")
    if not admin:
        return
    token = cfg["telegram_bot_token"]
    offset = int(state.get("tg_offset") or 0)
    data = http_get_json(f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0")
    handles = load_handles()
    changed = False

    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = msg.get("text") or ""
        if not text.startswith("/") or str((msg.get("from") or {}).get("id")) != admin:
            continue
        before = list(handles)
        reply = handle_command(cfg, handles, text)
        changed = changed or handles != before
        http_post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       {"chat_id": msg["chat"]["id"], "text": reply, "parse_mode": "HTML"})

    if changed:
        save_handles(handles)
    save_json(STATE_FILE, state)


def search_tweets(api_key, query):
    """Return all tweets for an advanced-search query (follows pagination)."""
    tweets, cursor = [], ""
    while True:
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        url = f"{TWITTERAPI_BASE}/twitter/tweet/advanced_search?{urllib.parse.urlencode(params)}"
        data = http_get_json(url, {"X-API-Key": api_key})
        tweets.extend(data.get("tweets") or [])
        if not data.get("has_next_page") or not data.get("next_cursor"):
            return tweets
        cursor = data["next_cursor"]
        time.sleep(6)  # stay under the free-tier request rate


def tweet_url(tweet):
    author = (tweet.get("author") or {}).get("userName", "i")
    return tweet.get("url") or f"https://x.com/{author}/status/{tweet.get('id')}"


def format_alert(tweet):
    author = tweet.get("author") or {}
    name = html.escape(author.get("name") or "")
    user = html.escape(author.get("userName") or "")
    followers = author.get("followers")
    followers_txt = f" ? {followers:,} followers" if isinstance(followers, int) else ""
    text = html.escape((tweet.get("text") or "")[:800])
    return f"🔔 <b>{name}</b> (@{user}){followers_txt}\n\n{text}"


def send_telegram(token, chat_id, text, url):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[{"text": "Open on X", "url": url}]]},
    }
    http_post_json(f"https://api.telegram.org/bot{token}/sendMessage", payload)


def print_chat_ids(token):
    data = http_get_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = {}
    for upd in data.get("result", []):
        chat = (upd.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or ""
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start / send /start, then retry.")
    for cid, name in chats.items():
        print(f"chat_id: {cid}   (@{name})")


def check_once(cfg, handles, state):
    since = int(state.get("last_check") or time.time() - 15 * 60)
    now = int(time.time())
    seen = set(state.get("seen_ids", []))
    new_tweets = []
    failed = False

    for i in range(0, len(handles), BATCH_SIZE):
        batch = handles[i:i + BATCH_SIZE]
        query = "(" + " OR ".join(f"from:{h}" for h in batch) + f") since_time:{since}"
        if not cfg.get("include_replies", False):
            query += " -filter:replies"
        try:
            new_tweets.extend(search_tweets(cfg["twitterapi_key"], query))
        except Exception as e:  # keep going with the other batches
            print(f"[search error] {e}")
            failed = True

    new_tweets = [t for t in new_tweets if str(t.get("id")) not in seen]
    new_tweets.sort(key=lambda t: int(t.get("id") or 0))  # oldest first

    for t in new_tweets:
        try:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], format_alert(t), tweet_url(t))
            seen.add(str(t.get("id")))
            time.sleep(3)  # stay under Telegram's per-channel limit
        except Exception as e:
            print(f"[telegram error] {e}")
            failed = True

    if not failed:  # on error, keep the old window so the next run catches up
        state["last_check"] = now
    state["seen_ids"] = list(seen)[-2000:]
    save_json(STATE_FILE, state)
    print(f"{time.strftime('%H:%M:%S')}  checked {len(handles)} accounts, {len(new_tweets)} new")


def main():
    cfg = load_json(CONFIG_FILE, {})
    # on GitHub Actions the keys come from repository secrets instead of config.json
    for key, env in [("twitterapi_key", "TWITTERAPI_KEY"),
                     ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
                     ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
                     ("telegram_admin_id", "TELEGRAM_ADMIN_ID")]:
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if not cfg.get("twitterapi_key") or not cfg.get("telegram_bot_token"):
        sys.exit("No keys found. Fill in config.json or set the environment variables.")

    if "--chat-id" in sys.argv:
        print_chat_ids(cfg["telegram_bot_token"])
        return

    state = load_json(STATE_FILE, {})
    interval = max(1, int(cfg.get("poll_minutes", 3))) * 60
    while True:
        try:
            process_commands(cfg, state)
        except Exception as e:  # a command problem must not stop the alerts
            print(f"[commands error] {e}")
        check_once(cfg, load_handles(), state)
        if "--once" in sys.argv:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
