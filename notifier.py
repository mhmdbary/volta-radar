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
STATS_FILE = ROOT / "stats.json"

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
    "/list  show tracked accounts\n"
    "/leaderboard  top Volta mentions and most active accounts\n\n"
    "<b>✍️ Writing</b>\n"
    "Send any idea (عربي or English) → I write the tweet\n"
    "/write idea  same thing\n"
    "Reply to a draft with your idea → I rewrite it\n"
    "post / انشر → link to X with it ready\n\n"
    "/myreport  your replies & posts, last 7 days (/myreport 1 for today)\n\n"
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

    if cmd == "/leaderboard":
        return build_leaderboard(tags)

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
        cb = upd.get("callback_query")
        if cb:
            if str((cb.get("from") or {}).get("id")) == admin:
                try:
                    handle_draft_button(cfg, state, cb)
                except Exception as e:
                    print(f"[draft button error] {e}")
            continue
        msg = upd.get("message") or {}
        text = msg.get("text") or ""
        if not text or str((msg.get("from") or {}).get("id")) != admin:
            continue
        try:
            replied_to = (msg.get("reply_to_message") or {}).get("message_id")
            first = text.split()[0].split("@")[0].lower()
            if text.strip().lower() in POST_WORDS or first == "/post":  # "post" / "انشر"
                post_link(cfg, state, replied_to)
                continue
            draft_key = find_draft(state, replied_to)[0] if replied_to else None
            if draft_key and not text.startswith("/"):  # you replied to a draft with your idea
                rewrite_draft(cfg, state, draft_key, text)
                continue
            if state.get("awaiting") and not text.startswith("/"):  # answer to the ✏️ button
                rewrite_draft(cfg, state, state.pop("awaiting"), text)
                continue
            if first == "/myreport":
                arg = text.partition(" ")[2].strip()
                days = int(arg) if arg.isdigit() else 7
                tg_call(cfg, "sendMessage", {"chat_id": msg["chat"]["id"], "parse_mode": "HTML",
                                             "disable_web_page_preview": True,
                                             "text": build_my_report(cfg, load_json(SETTINGS_FILE, {}), tags, days)})
                continue
            if first in ("/write", "/tweet"):
                new_tweet_draft(cfg, state, text.partition(" ")[2].strip())
                continue
            if not text.startswith("/") and cfg.get("anthropic_api_key"):  # any plain message = tweet idea
                new_tweet_draft(cfg, state, text.strip())
                continue
        except Exception as e:
            print(f"[ai command error] {e}")
            tg_call(cfg, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"⚠️ AI error: {e}"})
            continue
        if not text.startswith("/"):
            continue
        reply = handle_command(cfg, handles, tags, text)
        http_post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       {"chat_id": msg["chat"]["id"], "text": reply, "parse_mode": "HTML"})

    if handles != before_handles:
        save_handles(handles)
    if tags != before_tags:
        save_json(TAGS_FILE, tags)
    save_json(STATE_FILE, state)


def tg_call(cfg, method, payload):
    return http_post_json(f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/{method}", payload)


def wants_draft(tweet, tags, settings, mention):
    """Every tracked account and every mention gets read; the model still skips tweets where Volta isn't relevant."""
    if settings.get("ai_all_tracked", True):
        return True
    tag = (tags.get(((tweet.get("author") or {}).get("userName") or "").lower()) or "").lower()
    return mention or tag_marker(tag) == "🟢" or "hyperliquid" in tag or is_highlight(tweet, settings)


def draft_message(d, tags):
    reply_txt = html.escape(d["reply"])
    footer = ["", "✏️ <i>Reply to this message with your idea (عربي or English) and I'll rewrite it.</i>",
              "<i>Nothing is posted automatically.</i>"]
    meaning = [f"🇸🇦 <i>{html.escape(d['reply_ar'])}</i>"] if d.get("reply_ar") else []
    if d.get("kind") == "tweet":
        return "\n".join([
            "📝 <b>TWEET DRAFT</b>", LINE,
            f"🗒 <i>{html.escape(d.get('instruction') or '')}</i>", LINE,
            "✍️ <b>Tweet</b> (tap to copy):", f"<code>{reply_txt}</code>"] + meaning + footer)
    snippet = html.escape(clean_text(d["text"], drop_links=True)[:300]) or "📷 media post"
    label = "🟢 <b>Mentions Volta</b>" if d.get("relevant") else "💬 <b>Engagement only</b> (no Volta mention)"
    lines = ["🤖 <b>REPLY IDEA</b>", LINE,
             tag_line(tags, d["author"]),
             f"👤 <code>@{html.escape(d['author'])}</code>",
             f"<blockquote>{snippet}</blockquote>"]
    if d.get("tweet_ar"):
        lines.append(f"🇸🇦 <b>الترجمة:</b>\n<blockquote>{html.escape(d['tweet_ar'][:400])}</blockquote>")
    lines += [LINE, label, "✍️ <b>Suggested reply</b> (tap to copy):", f"<code>{reply_txt}</code>"] + meaning
    if d.get("instruction"):
        lines.append(f"🗒 <i>Your idea: {html.escape(d['instruction'])}</i>")
    return "\n".join(lines + footer)


def draft_buttons(tid, d):
    if d.get("kind") == "tweet":
        first = {"text": "📝 Post on X", "url": "https://x.com/intent/post?" + urllib.parse.urlencode({"text": d["reply"]})}
    else:
        first = {"text": "✍️ Reply on X",
                 "url": "https://x.com/intent/post?" + urllib.parse.urlencode({"in_reply_to": tid, "text": d["reply"]})}
    return {"inline_keyboard": [
        [first],
        [{"text": "✏️ اكتب فكرتك بالعربي", "callback_data": f"ins:{tid}"}],
        [{"text": "🔄 Change", "callback_data": f"chg:{tid}"},
         {"text": "❌ Ignore", "callback_data": f"ign:{tid}"}],
    ]}


def apply_result(d, result):
    d["reply"] = result["reply"]
    d["relevant"] = result.get("relevant", d.get("relevant"))
    d["reply_ar"] = result.get("reply_ar") or ""
    d["tweet_ar"] = result.get("tweet_ar") or d.get("tweet_ar") or ""
    d["tried"] = (d.get("tried", []) + [d["reply"]])[-5:]


def regenerate(cfg, d, avoid=None):
    import ai_replies
    if d.get("kind") == "tweet":
        return ai_replies.write_tweet(cfg["anthropic_api_key"], d.get("instruction"), avoid=avoid)
    return ai_replies.suggest_reply(cfg["anthropic_api_key"], d["author"], d["text"],
                                    avoid=avoid, instruction=d.get("instruction"), image=d.get("image"))


def store_draft(state, key, d):
    drafts = state.setdefault("drafts", {})
    drafts[key] = d
    for old in list(drafts)[:-100]:  # keep the newest 100
        drafts.pop(old)


def show_draft(cfg, tags, key, d, edit_message_id=None):
    payload = {"chat_id": cfg["telegram_admin_id"], "text": draft_message(d, tags), "parse_mode": "HTML",
               "reply_markup": draft_buttons(key, d)}
    if d.get("image"):  # show the tweet's image above the draft (text edits keep working, unlike photo captions)
        payload["link_preview_options"] = {"url": d["image"], "prefer_large_media": True, "show_above_text": True}
    else:
        payload["link_preview_options"] = {"is_disabled": True}
    if edit_message_id:
        tg_call(cfg, "editMessageText", dict(payload, message_id=edit_message_id))
    else:
        d["message_id"] = tg_call(cfg, "sendMessage", payload)["result"]["message_id"]


def find_draft(state, message_id):
    return next(((k, v) for k, v in state.get("drafts", {}).items() if v.get("message_id") == message_id),
                (None, None))


def rewrite_draft(cfg, state, key, instruction):
    """You gave your own idea for a draft (by replying to it or via the ✏️ button): rewrite it."""
    d = state.get("drafts", {}).get(key)
    if not d:
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"],
                                     "text": "That draft expired. Use /write your idea for a new tweet."})
        return
    d["instruction"] = instruction
    result = regenerate(cfg, d)
    if not result or not result.get("reply"):
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"], "text": "Couldn't write that one, try rephrasing."})
        return
    apply_result(d, result)
    old_id = d.get("message_id")
    show_draft(cfg, load_json(TAGS_FILE, {}), key, d)  # send fresh at the bottom so you see it
    if old_id:
        try:
            tg_call(cfg, "deleteMessage", {"chat_id": cfg["telegram_admin_id"], "message_id": old_id})
        except Exception:
            pass


def new_tweet_draft(cfg, state, instruction):
    """/write your idea: draft a new tweet from your instruction."""
    if not instruction:
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"], "parse_mode": "HTML",
                                     "text": "Usage: <code>/write your idea</code>\nمثال: <code>/write تويتة عن إطلاق elysium يوم 9، حماسية</code>"})
        return
    d = {"kind": "tweet", "instruction": instruction, "reply": "", "tried": []}
    result = regenerate(cfg, d)
    if not result or not result.get("reply"):
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"], "text": "Couldn't write that one, try rephrasing."})
        return
    apply_result(d, result)
    key = f"w{int(time.time())}"
    show_draft(cfg, {}, key, d)
    store_draft(state, key, d)


def send_draft(cfg, settings, state, tags, tweet, mention):
    """Ask Claude for a reply idea and DM it to the admin if the tweet is relevant."""
    if not cfg.get("anthropic_api_key") or not cfg.get("telegram_admin_id"):
        return
    if not wants_draft(tweet, tags, settings, mention):
        return
    author = (tweet.get("author") or {}).get("userName") or ""
    d = {"author": author, "text": tweet.get("text") or "", "reply": "", "tried": [],
         "image": tweet_media(tweet)[0]}
    result = regenerate(cfg, d)
    if not result or not result.get("reply"):
        return
    apply_result(d, result)
    tid = str(tweet.get("id"))
    show_draft(cfg, tags, tid, d)
    store_draft(state, tid, d)


POST_WORDS = {"post", "انشر", "نشر", "publish", "send", "ابعت"}


def post_link(cfg, state, message_id=None):
    """You said 'post': send the X link with the draft (latest one, or the one you replied to) ready."""
    drafts = state.get("drafts", {})
    found = [(k, v) for k, v in drafts.items() if not message_id or v.get("message_id") == message_id]
    if not found:
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"], "text": "No draft to post yet."})
        return
    key, d = found[-1]
    button = draft_buttons(key, d)["inline_keyboard"][0][0]
    tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"], "parse_mode": "HTML",
                                 "text": f"🚀 <b>Ready to post</b>\n\n<code>{html.escape(d['reply'])}</code>\n\n"
                                         "<i>Tap the button, check it, then press Post on X.</i>",
                                 "reply_markup": {"inline_keyboard": [[button]]}})


def handle_draft_button(cfg, state, cb):
    action, _, key = (cb.get("data") or "").partition(":")
    d = state.get("drafts", {}).get(key)
    msg_id = (cb.get("message") or {}).get("message_id")
    if not d:
        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "This draft expired."})
        return
    if action == "ign":
        state["drafts"].pop(key, None)
        who = f"@{html.escape(d['author'])}" if d.get("author") else "tweet draft"
        tg_call(cfg, "editMessageText", {"chat_id": cfg["telegram_admin_id"], "message_id": msg_id,
                                         "text": f"❌ <i>Ignored · {who}</i>", "parse_mode": "HTML"})
        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Ignored"})
        return
    if action == "ins":
        state["awaiting"] = key
        who = f"للرد على @{d['author']}" if d.get("author") else "للتويتة"
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"],
                                     "text": f"✏️ اكتب فكرتك {who} بالعربي (أو إنجليزي)، وأنا بحولها لإنجليزي طبيعي.",
                                     "reply_markup": {"force_reply": True, "input_field_placeholder": "اكتب فكرتك هون..."}})
        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "اكتب فكرتك"})
        return
    if action == "chg":
        result = regenerate(cfg, d, avoid=d.get("tried"))
        if not result or not result.get("reply"):
            tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "No better idea, sorry"})
            return
        apply_result(d, result)
        show_draft(cfg, load_json(TAGS_FILE, {}), key, d, edit_message_id=msg_id)
        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "New idea ready"})


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


def tweet_media(tweet):
    """Return (image_url, kind) for the tweet's first photo/video thumbnail, or (None, None)."""
    for source in (tweet.get("extendedEntities"), tweet.get("entities")):
        for m in (source or {}).get("media") or []:
            if m.get("media_url_https"):
                return m["media_url_https"], m.get("type") or "photo"
    return None, None


def format_alert(tweet, settings, tags, mention=False):
    author = tweet.get("author") or {}
    handle = author.get("userName") or ""
    name = html.escape(author.get("name") or handle)
    image, kind = tweet_media(tweet)
    # photo captions are capped at 1024 characters by Telegram, so keep the quote shorter there
    text = html.escape(clean_text(tweet.get("text"), drop_links=bool(image))[:450 if image else 700])
    if not text:
        text = "🎥 <i>video post</i>" if kind in ("video", "animated_gif") else "📷 <i>media post</i>"
    elif kind in ("video", "animated_gif"):
        text = f"🎥 {text}"

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


def send_telegram(token, chat_id, text, url=None, profile=None, photo=None):
    payload = {"chat_id": chat_id, "parse_mode": "HTML"}
    buttons = []
    if url:
        buttons.append({"text": "🔗 Open on X", "url": url})
    if profile:
        buttons.append({"text": "👤 Profile", "url": f"https://x.com/{profile}"})
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [buttons]}
    if photo:
        try:
            http_post_json(f"https://api.telegram.org/bot{token}/sendPhoto",
                           dict(payload, photo=photo, caption=text[:1024]))
            return
        except urllib.error.HTTPError as e:  # bad image or caption too long: fall back to text
            print(f"[photo fallback] {e}")
    http_post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                   dict(payload, text=text, disable_web_page_preview=True))


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


def record_stats(tweets):
    """Count posts per tracked account and Volta mentions per author, for the leaderboard."""
    stats = load_json(STATS_FILE, {})
    stats.setdefault("since", time.strftime("%Y-%m-%d", time.gmtime()))
    posts, mentions = stats.setdefault("posts", {}), stats.setdefault("mentions", {})
    followers = stats.setdefault("followers", {})
    for t, mention in tweets:
        author = t.get("author") or {}
        handle = author.get("userName")
        if not handle:
            continue
        if isinstance(author.get("followers"), int):
            followers[handle] = author["followers"]
        if not mention:
            posts[handle] = posts.get(handle, 0) + 1
        if mention or "volta" in (t.get("text") or "").lower():
            mentions[handle] = mentions.get(handle, 0) + 1
    save_json(STATS_FILE, stats)


def build_leaderboard(tags):
    stats = load_json(STATS_FILE, {})
    since = stats.get("since")
    date = time.strftime("%b %d", time.strptime(since, "%Y-%m-%d")) if since else "start"
    followers = stats.get("followers", {})

    def rows(counts, unit):
        top = sorted(counts.items(), key=lambda kv: (-kv[1], -followers.get(kv[0], 0)))[:5]
        if not top:
            return ["<i>Nothing yet.</i>"]
        return [f"{medal} {tag_marker(tags.get(h.lower()))} <a href=\"https://x.com/{h}\"><b>@{html.escape(h)}</b></a>"
                f" · {n} {unit}{'s' if n != 1 else ''} · 👥 {short_num(followers.get(h))}"
                for medal, (h, n) in zip(MEDALS, top)]

    lines = ["🏆 <b>VOLTA LEADERBOARD</b>", LINE,
             "💬 <b>MOST VOLTA MENTIONS</b>", ""] + rows(stats.get("mentions", {}), "mention") + [
             LINE, "🔥 <b>MOST ACTIVE ACCOUNTS</b>", ""] + rows(stats.get("posts", {}), "post") + [
             LINE, f"📅 Since {date}", "⚡ #Leaderboard #VoltaRadar"]
    return "\n".join(lines)


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
            min_followers = int(settings.get("mention_min_followers", 0))
            for t in search_tweets(cfg["twitterapi_key"], query):
                author = t.get("author") or {}
                if (author.get("userName") or "").lower() in exclude:
                    continue
                if int(author.get("followers") or 0) < min_followers:  # skip tiny/bot accounts
                    continue
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
    record_stats(ordered)

    for t, mention in ordered:
        try:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                          format_alert(t, settings, tags, mention), tweet_url(t),
                          (t.get("author") or {}).get("userName"), tweet_media(t)[0])
            seen.add(str(t.get("id")))
            time.sleep(3)  # stay under Telegram's per-channel limit
        except Exception as e:
            print(f"[telegram error] {e}")
            failed = True
            continue
        try:
            send_draft(cfg, settings, state, tags, t, mention)
        except Exception as e:  # AI problems must never block alerts
            print(f"[draft error] {e}")

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

    tags = load_json(TAGS_FILE, {})
    send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                  build_digest(settings, tags, tweets, today, len(handles)))
    time.sleep(2)
    send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], build_leaderboard(tags))
    if cfg.get("telegram_admin_id") and settings.get("my_handle"):  # your daily work log, DM only
        try:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_admin_id"],
                          build_my_report(cfg, settings, tags, 1))
        except Exception as e:
            print(f"[my report error] {e}")
    state["last_digest"] = today
    save_json(STATE_FILE, state)
    print("daily report sent")


def build_my_report(cfg, settings, tags, days):
    """Your own X activity: replies and posts with likes/views, to show the work you've done."""
    me = (settings.get("my_handle") or "").lstrip("@")
    if not me:
        return "Set <code>my_handle</code> in settings.json first."
    since = int(time.time()) - days * 86400
    tweets = search_tweets(cfg["twitterapi_key"], f"from:{me} since_time:{since} -filter:retweets")
    is_reply = lambda t: bool(t.get("isReply") or t.get("inReplyToUsername"))
    replies = [t for t in tweets if is_reply(t)]
    posts = [t for t in tweets if not is_reply(t)]

    def n(t, k):
        return int(t.get(k) or 0)

    likes = sum(n(t, "likeCount") for t in tweets)
    views = sum(n(t, "viewCount") for t in tweets)
    period = "TODAY" if days == 1 else f"LAST {days} DAYS"
    lines = [f"📋 <b>MY WORK · {period}</b>", LINE,
             f"💬 {len(replies)} replies   📝 {len(posts)} posts",
             f"❤️ {short_num(likes)} likes   👁 {short_num(views)} views"]

    by_cat = {}
    for t in replies:
        marker = tag_marker(tags.get((t.get("inReplyToUsername") or "").lower()))
        by_cat[marker] = by_cat.get(marker, 0) + 1
    if by_cat:
        names = {"🟢": "ecosystem", "🔵": "networks", "🟣": "KOLs", "⚪": "others"}
        lines.append("   ".join(f"{m} {c} {names[m]}" for m, c in sorted(by_cat.items(), key=lambda kv: -kv[1])))

    top = sorted(tweets, key=lambda t: (n(t, "likeCount"), n(t, "viewCount")), reverse=True)[:5]
    if top:
        lines += [LINE, "🏆 <b>TOP</b>", ""]
        for medal, t in zip(MEDALS, top):
            to = t.get("inReplyToUsername")
            where = f"↪️ reply to @{html.escape(to)}" if to else "📝 post"
            snippet = html.escape(clean_text(t.get("text"), drop_links=True).replace("\n", " ")[:90])
            lines += [f"{medal} <a href=\"{tweet_url(t)}\">{where}</a>",
                      f"❤️ {n(t, 'likeCount')}   👁 {short_num(n(t, 'viewCount'))}   💬 {n(t, 'replyCount')}",
                      f"<blockquote>{snippet}</blockquote>"]
    if replies:
        lines += [LINE, "🔗 <b>ALL REPLIES</b> (tap to copy)"]
        for t in sorted(replies, key=lambda t: int(t.get("id") or 0))[-20:]:
            lines.append(f"• @{html.escape(t.get('inReplyToUsername') or '?')}: <code>{tweet_url(t)}</code>")
    if not tweets:
        lines.append("\n<i>Nothing posted in this period yet.</i>")
    lines += [LINE, "⚡ #MyWork"]
    return "\n".join(lines)


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
                     ("telegram_admin_id", "TELEGRAM_ADMIN_ID"),
                     ("anthropic_api_key", "ANTHROPIC_API_KEY")]:
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
        if "--commands-only" in sys.argv:  # quick pass for button presses between full checks
            save_json(STATE_FILE, state)
            return
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
