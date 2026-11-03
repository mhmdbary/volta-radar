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
SETTINGS_FILE = ROOT / "settings.json"
TAGS_FILE = ROOT / "tags.json"

# tags containing one of these get the green "our ecosystem" marker
ECO_WORDS = ("volta", "elysium", "kinetiq")

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
    """Return (username, followers, bio) for the account, or None if it doesn't exist."""
    url = f"{TWITTERAPI_BASE}/twitter/user/info?{urllib.parse.urlencode({'userName': handle})}"
    try:
        data = http_get_json(url, {"X-API-Key": api_key}).get("data") or {}
    except Exception:
        return None
    if not data.get("userName"):
        return None
    return data["userName"], data.get("followers"), data.get("description") or ""


def guess_tag(bio):
    bio = bio.lower()
    if "elysium" in bio or "kinetiq" in bio:
        return "Elysium · ecosystem"
    if "hyperliquid" in bio:
        return "Hyperliquid"
    return "KOL"


LINE = "━━━━━━━━━━━━━━━━━━"
MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def tag_marker(tag):
    """🟢 our ecosystem · 🟣 KOL · 🔵 other network/project · ⚪ untracked."""
    if not tag:
        return "⚪"
    low = tag.lower()
    if any(w in low for w in ECO_WORDS):
        return "🟢"
    if "kol" in low:
        return "🟣"
    return "🔵"


def tag_line(tags, handle):
    tag = tags.get((handle or "").lower())
    return f"{tag_marker(tag)} <b>{html.escape((tag or 'Community').upper())}</b>"


def tag_hashtag(tags, handle):
    """Category as a hashtag, so the channel can be searched by network (#Elysium, #KOL)."""
    tag = tags.get((handle or "").lower()) or ""
    if not tag:
        return "#Community"
    if "kol" in tag.lower():
        return "#KOL"
    word = "".join(c for c in tag.split("·")[0].split("/")[0].strip().split(" ")[0] if c.isalnum())
    return f"#{word}" if word else ""


def short_num(n):
    if not isinstance(n, int):
        return "?"
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def clean_text(text, drop_links=False):
    text = (text or "").strip()
    if drop_links:
        text = " ".join(w for w in text.split() if not w.startswith("https://t.co/"))
    return text


HELP_TEXT = (
    "<b>Volta Radar commands</b>\n\n"
    "/add username  (or @username or x.com link)\n"
    "/remove username\n"
    "/tag username Some Tag  set the label shown on alerts\n"
    "/list  show tracked accounts\n\n"
    "Commands are read every few minutes, so replies can take a short while."
)


def handle_command(cfg, handles, tags, text):
    """Apply one admin command to the handles list and tags; return the reply text."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    lower = [h.lower() for h in handles]

    if cmd == "/list":
        lines = "\n".join(f"{i}. @{html.escape(h)} · {html.escape(tags.get(h.lower(), '-'))}"
                          for i, h in enumerate(handles, 1))
        return f"<b>Tracking {len(handles)} accounts</b>\n\n{lines}"

    if cmd == "/tag":
        bits = arg.split(maxsplit=1)
        if len(bits) < 2:
            return "Usage: /tag username Some Tag"
        name = parse_handle(bits[0])
        tags[name.lower()] = bits[1].strip()
        return f"🏷 @{html.escape(name)} → {html.escape(bits[1].strip())}"

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
                real, followers, bio = found
                handles.append(real)
                lower.append(real.lower())
                tag = tags.setdefault(real.lower(), guess_tag(bio))
                f = f" ({followers:,} followers)" if isinstance(followers, int) else ""
                replies.append(f"✅ Added @{html.escape(real)}{f} · 🏷 {html.escape(tag)}")
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
    tags = load_json(TAGS_FILE, {})
    before_handles, before_tags = list(handles), dict(tags)

    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = msg.get("text") or ""
        if not text.startswith("/") or str((msg.get("from") or {}).get("id")) != admin:
            continue
        reply = handle_command(cfg, handles, tags, text)
        http_post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       {"chat_id": msg["chat"]["id"], "text": reply, "parse_mode": "HTML"})

    if handles != before_handles:
        save_handles(handles)
    if tags != before_tags:
        save_json(TAGS_FILE, tags)
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


def is_highlight(tweet, settings):
    text = (tweet.get("text") or "").lower()
    return any(w.lower() in text for w in settings.get("highlight_words", []))


def format_alert(tweet, settings, tags, mention=False):
    author = tweet.get("author") or {}
    handle = author.get("userName") or ""
    name = html.escape(author.get("name") or handle)
    text = html.escape(clean_text(tweet.get("text"))[:700]) or "📷 <i>media post</i>"

    top = []
    if mention:
        top.append("💬 <b>$VOLTA MENTION</b>")
    elif is_highlight(tweet, settings):
        top.append("⭐ <b>IMPORTANT</b> ⭐")
    top.append(tag_line(tags, handle))

    parts = top + [LINE,
                   f"👤 <b>{name}</b>  <code>@{html.escape(handle)}</code>",
                   f"👥 {short_num(author.get('followers'))} followers",
                   "",
                   f"<blockquote>{text}</blockquote>",
                   LINE]
    tags_txt = " ".join(x for x in (tag_hashtag(tags, handle), "#VoltaRadar") if x)
    parts.append(f"⚡ {tags_txt}")
    return "\n".join(parts)


def send_telegram(token, chat_id, text, url=None, profile=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    buttons = []
    if url:
        buttons.append({"text": "🔗 Open on X", "url": url})
    if profile:
        buttons.append({"text": "👤 Profile", "url": f"https://x.com/{profile}"})
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [buttons]}
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


def mention_query(settings, handles, since):
    """Search for Volta mentions by anyone not already tracked (their posts arrive as normal alerts)."""
    terms = settings.get("mention_terms") or []
    if not terms:
        return None
    exclude = set(h.lower() for h in handles) | set(h.lower() for h in settings.get("mention_exclude", []))
    query = "(" + " OR ".join(terms) + f") since_time:{since} -filter:retweets"
    # keep the query short: only exclude the explicit list, tracked authors are filtered after
    query += "".join(f" -from:{h}" for h in settings.get("mention_exclude", []))
    return query, exclude


def check_once(cfg, settings, handles, state):
    since = int(state.get("last_check") or time.time() - 15 * 60)
    now = int(time.time())
    seen = set(state.get("seen_ids", []))
    new_tweets = []  # (tweet, is_mention)
    failed = False

    for i in range(0, len(handles), BATCH_SIZE):
        batch = handles[i:i + BATCH_SIZE]
        query = "(" + " OR ".join(f"from:{h}" for h in batch) + f") since_time:{since}"
        if not cfg.get("include_replies", False):
            query += " -filter:replies"
        try:
            new_tweets.extend((t, False) for t in search_tweets(cfg["twitterapi_key"], query))
        except Exception as e:  # keep going with the other batches
            print(f"[search error] {e}")
            failed = True

    mq = mention_query(settings, handles, since)
    if mq:
        query, exclude = mq
        try:
            for t in search_tweets(cfg["twitterapi_key"], query):
                if ((t.get("author") or {}).get("userName") or "").lower() not in exclude:
                    new_tweets.append((t, True))
        except Exception as e:
            print(f"[mention search error] {e}")
            failed = True

    unique = {}
    for t, mention in new_tweets:
        tid = str(t.get("id"))
        if tid not in seen and tid not in unique:
            unique[tid] = (t, mention)
    ordered = sorted(unique.values(), key=lambda x: int(x[0].get("id") or 0))  # oldest first
    tags = load_json(TAGS_FILE, {})

    for t, mention in ordered:
        try:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                          format_alert(t, settings, tags, mention), tweet_url(t),
                          (t.get("author") or {}).get("userName"))
            seen.add(str(t.get("id")))
            time.sleep(3)  # stay under Telegram's per-channel limit
        except Exception as e:
            print(f"[telegram error] {e}")
            failed = True

    if not failed:  # on error, keep the old window so the next run catches up
        state["last_check"] = now
    state["seen_ids"] = list(seen)[-2000:]
    save_json(STATE_FILE, state)
    mentions = sum(1 for _, m in ordered if m)
    print(f"{time.strftime('%H:%M:%S')}  checked {len(handles)} accounts, "
          f"{len(ordered) - mentions} new posts, {mentions} mentions")


def engagement(t):
    return sum(int(t.get(k) or 0) * w for k, w in
               [("likeCount", 1), ("retweetCount", 3), ("replyCount", 2), ("quoteCount", 3)])


def countdown_line(settings, today):
    try:
        launch = time.strptime(settings["launch_date"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return ""
    days = (time.mktime(launch) - time.mktime(time.strptime(today, "%Y-%m-%d"))) // 86400
    name = html.escape(settings.get("launch_name", "Launch"))
    if days > 1:
        return f"⏳ <b>{name.upper()} IN {int(days)} DAYS</b>\n📅 {settings['launch_date']}"
    if days == 1:
        return f"⏳ <b>{name.upper()} IS TOMORROW</b> 🚀"
    if days == 0:
        return f"🚀 <b>{name.upper()} IS LIVE TODAY!</b> 🚀"
    return ""


def build_digest(settings, tags, tweets, today, n_accounts):
    top = sorted(tweets, key=engagement, reverse=True)[:5]
    date = time.strftime("%b %d", time.strptime(today, "%Y-%m-%d"))
    lines = [f"📡 <b>DAILY RADAR</b> · {date}", LINE]
    countdown = countdown_line(settings, today)
    if countdown:
        lines += [countdown, LINE]
    lines += ["🔥 <b>TOP POSTS TODAY</b>", ""]
    for medal, t in zip(MEDALS, top):
        handle = (t.get("author") or {}).get("userName") or ""
        snippet = html.escape(clean_text(t.get("text"), drop_links=True).replace("\n", " ")[:110]) or "📷 media post"
        marker = tag_marker(tags.get(handle.lower()))
        lines += [f"{medal} {marker} <a href=\"{tweet_url(t)}\"><b>@{html.escape(handle)}</b></a>",
                  f"❤️ {short_num(int(t.get('likeCount') or 0))}   🔁 {short_num(int(t.get('retweetCount') or 0))}"
                  f"   💬 {short_num(int(t.get('replyCount') or 0))}",
                  f"<blockquote>{snippet}</blockquote>"]
    if not top:
        lines.append("<i>No posts in the last 24h.</i>")
    lines += [LINE, f"📈 {len(tweets)} posts · {n_accounts} accounts tracked", "⚡ #DailyRadar #VoltaRadar"]
    return "\n".join(lines)


def daily_report(cfg, settings, handles, state):
    """Once a day: launch countdown + the 5 most-engaged posts from tracked accounts."""
    now = time.gmtime()
    today = time.strftime("%Y-%m-%d", now)
    if now.tm_hour < int(settings.get("daily_hour_utc", 17)) or state.get("last_digest") == today:
        return

    since = int(time.time()) - 86400
    tweets = []
    for i in range(0, len(handles), BATCH_SIZE):
        batch = handles[i:i + BATCH_SIZE]
        query = "(" + " OR ".join(f"from:{h}" for h in batch) + f") since_time:{since} -filter:replies"
        tweets.extend(search_tweets(cfg["twitterapi_key"], query))

    send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                  build_digest(settings, load_json(TAGS_FILE, {}), tweets, today, len(handles)))
    state["last_digest"] = today
    save_json(STATE_FILE, state)
    print("daily report sent")


def get_followings(api_key, account):
    names, cursor = [], ""
    while True:
        params = {"userName": account, "pageSize": 200}
        if cursor:
            params["cursor"] = cursor
        data = http_get_json(f"{TWITTERAPI_BASE}/twitter/user/followings?{urllib.parse.urlencode(params)}",
                             {"X-API-Key": api_key})
        names.extend(u for u in data.get("followings") or [] if u.get("userName"))
        if not data.get("has_next_page") or not data.get("next_cursor"):
            return names
        cursor = data["next_cursor"]


def format_new_follow(account, u, already_tracked):
    bio = html.escape(clean_text(u.get("description")).replace("\n", " ")[:200]) or "<i>no bio</i>"
    user = html.escape(u["userName"])
    followers = u.get("followers_count", u.get("followers"))
    lines = ["🆕 <b>NEW ON THE RADAR</b>", LINE,
             f"🟢 <b>@{html.escape(account)}</b> just followed",
             f"👤 <b>{html.escape(u.get('name') or user)}</b>  <code>@{user}</code>",
             f"👥 {short_num(followers if isinstance(followers, int) else None)} followers",
             "", f"<blockquote>{bio}</blockquote>", LINE]
    if not already_tracked:
        lines.append(f"➕ Track it: <code>/add {user}</code>")
    lines.append("⚡ #NewFollow #VoltaRadar")
    return "\n".join(lines)


def check_new_follows(cfg, settings, handles, state):
    """Every few hours: alert when the official accounts follow someone new (new projects/partners)."""
    every = float(settings.get("follow_check_hours", 6)) * 3600
    if time.time() - float(state.get("last_follow_check") or 0) < every:
        return
    known = state.setdefault("known_follows", {})
    tracked = set(h.lower() for h in handles)

    for account in settings.get("watch_follows_of", []):
        users = get_followings(cfg["twitterapi_key"], account)
        current = [u["userName"] for u in users]
        if account in known:  # first run only records the list, so we don't flood the channel
            old = set(n.lower() for n in known[account])
            for u in users:
                if u["userName"].lower() in old:
                    continue
                send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                              format_new_follow(account, u, u["userName"].lower() in tracked),
                              profile=u["userName"])
                time.sleep(3)
        known[account] = current

    state["last_follow_check"] = int(time.time())
    save_json(STATE_FILE, state)


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
        settings = load_json(SETTINGS_FILE, {})
        handles = load_handles()
        check_once(cfg, settings, handles, state)
        # extras must never stop the main alerts
        for extra in (daily_report, check_new_follows):
            try:
                extra(cfg, settings, handles, state)
            except Exception as e:
                print(f"[{extra.__name__} error] {e}")
        if "--once" in sys.argv:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
