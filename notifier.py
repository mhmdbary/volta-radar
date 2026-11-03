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
BATCH_SIZE = 25  # handles per search query (one paid request per batch)


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
    for line in HANDLES_FILE.read_text(encoding="utf-8").splitlines():
        h = line.strip().lstrip("@")
        if h and not h.startswith("#"):
            handles.append(h)
    return handles


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
    followers_txt = f" · {followers:,} followers" if isinstance(followers, int) else ""
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
                     ("telegram_chat_id", "TELEGRAM_CHAT_ID")]:
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if not cfg.get("twitterapi_key") or not cfg.get("telegram_bot_token"):
        sys.exit("No keys found. Fill in config.json or set the environment variables.")

    if "--chat-id" in sys.argv:
        print_chat_ids(cfg["telegram_bot_token"])
        return

    handles = load_handles()
    state = load_json(STATE_FILE, {})
    if "--once" in sys.argv:
        check_once(cfg, handles, state)
        return

    interval = max(1, int(cfg.get("poll_minutes", 3))) * 60
    while True:
        check_once(cfg, handles, state)
        time.sleep(interval)


if __name__ == "__main__":
    main()
