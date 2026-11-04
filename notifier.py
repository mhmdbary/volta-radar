"""
Alert-only Telegram notifier: when any account in handles.txt posts a new tweet,
send a Telegram message with the text and a link. It never posts or replies on X.

Usage:
    python notifier.py --chat-id   # print your Telegram chat id (send /start to the bot first)
    python notifier.py --once      # check once and exit
    python notifier.py             # keep running, checking every poll_minutes
"""
import calendar
import html
import json
import os
import re
import subprocess
import sys
import threading
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
MYWORK_FILE = ROOT / "mywork.json"

# tags containing one of these get the green "our ecosystem" marker
ECO_WORDS = ("volta", "elysium", "kinetiq")

TWITTERAPI_BASE = "https://api.twitterapi.io"
BATCH_SIZE = 20  # handles per search query (one paid request per batch)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


STATE_LOCK = threading.RLock()  # the command thread and the checker share one state dict


def save_json(path, data):
    with STATE_LOCK:
        for _ in range(3):  # the other thread may add a key mid-dump; just try again
            try:
                text = json.dumps(data, ensure_ascii=False, indent=2)
                break
            except RuntimeError:
                time.sleep(0.05)
        else:
            return
        path.write_text(text, encoding="utf-8")


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


# Telegram turns a bare @name into a Telegram user link; point every @handle at its X profile instead.
# Text already inside <a>...</a> or <code>...</code> is left alone (links can't nest, code can't hold links);
# handles inside <b>, <i>, <blockquote> etc. are linked too. Word characters before "@" mean an email, skip.
X_HANDLE = re.compile(r"(?<![\w/@])@(\w{1,15})(?!\w)")
KEEP_AS_IS = re.compile(r"(<a\b[^>]*>.*?</a>|<code>.*?</code>|<[^>]+>)", re.S)


def link_x_handles(text):
    parts = KEEP_AS_IS.split(text)  # odd indexes are tags / links / code, even indexes are plain text
    for i in range(0, len(parts), 2):
        parts[i] = X_HANDLE.sub(lambda m: f'<a href="https://x.com/{m.group(1)}">@{m.group(1)}</a>', parts[i])
    return "".join(parts)


def http_post_json(url, payload, retries=3):
    if "api.telegram.org" in url and payload.get("parse_mode") == "HTML":
        payload = dict(payload)
        for field in ("text", "caption"):
            if isinstance(payload.get(field), str):
                payload[field] = link_x_handles(payload[field])
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


def strip_leading_mentions(text):
    """X puts "@a @b @c" in front of every reply; drop that so only the actual message shows."""
    words = (text or "").split(" ")
    while words and (words[0].startswith("@") or words[0] == ""):
        words.pop(0)
    return " ".join(words).strip()


RLM = "‏"  # right-to-left mark: keeps Arabic lines from getting scrambled by English words / $VOLTA


def arabic_quote(text):
    lines = strip_leading_mentions(text).split("\n")
    return "<blockquote expandable>🇸🇦 " + "\n".join(RLM + html.escape(l) for l in lines) + "</blockquote>"


HELP_TEXT = (
    "⚡ <b>VOLTA RADAR</b>\n\n"
    "📊 <b>Reports</b>\n"
    "/mywork · /today · /week · /month\n"
    "/links · all your Volta tweet links\n"
    "/leaderboard\n\n"
    "✍️ <b>Writing</b>\n"
    "Send any idea (عربي / English) → English tweet\n"
    "Reply to a draft with your idea → rewritten\n"
    "post / انشر → opens X with it ready\n\n"
    "📃 <b>Accounts</b>\n"
    "/list · /add name · /remove name\n"
    "/tag name Label\n\n"
    "/menu · show the buttons"
)


def handle_command(cfg, handles, tags, text):
    """Apply one admin command to the handles list and tags; return the reply text."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    lower = [h.lower() for h in handles]

    if cmd == "/list":
        groups = {"🟢": [], "🔵": [], "🟣": [], "⚪": []}
        for h in handles:
            groups[tag_marker(tags.get(h.lower()))].append(h)
        names = {"🟢": "OUR ECOSYSTEM", "🔵": "NETWORKS & PROJECTS", "🟣": "KOLs", "⚪": "OTHER"}
        out = [f"📃 <b>TRACKED ACCOUNTS</b> · {len(handles)}"]
        for marker, members in groups.items():
            if members:
                out += ["", f"{marker} <b>{names[marker]}</b> · {len(members)}",
                        "  ".join(f"@{html.escape(h)}" for h in sorted(members, key=str.lower))]
        return "\n".join(out)

    if cmd == "/leaderboard":
        return build_leaderboard(cfg, tags, load_json(SETTINGS_FILE, {}))

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


MENU_ACTIONS = {
    "✍️ تويتة جديدة": "write",
    "📋 شغلي": "report",
    "🏆 الترتيب": "leaderboard",
    "📃 الحسابات": "list",
    "➕ ضيف حساب": "add",
    "➖ شيل حساب": "remove",
    "🔗 روابطي": "links",
}
MENU_KEYBOARD = {
    "keyboard": [[{"text": "✍️ تويتة جديدة"}, {"text": "📋 شغلي"}],
                 [{"text": "🏆 الترتيب"}, {"text": "📃 الحسابات"}],
                 [{"text": "➕ ضيف حساب"}, {"text": "➖ شيل حساب"}],
                 [{"text": "🔗 روابطي"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
REPORT_BUTTONS = {"inline_keyboard": [[{"text": "Today", "callback_data": "rep:1"},
                                       {"text": "7 days", "callback_data": "rep:7"},
                                       {"text": "30 days", "callback_data": "rep:30"},
                                       {"text": "All", "callback_data": "rep:3650"}]]}
ASK = {
    "write": "✍️ اكتب فكرة التويتة بالعربي، وأنا بكتبها إنجليزي:",
    "add": "➕ اكتب يوزر الحساب أو الرابط تبعه:",
    "remove": "➖ اكتب يوزر الحساب اللي بدك تشيله:",
}


def show_menu(cfg):
    tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"], "reply_markup": MENU_KEYBOARD,
                                 "text": "⚡ أهلاً! كل شيء من الأزرار تحت 👇\n\n"
                                         "اقتراحات الرد بتوصلك هون لحالها مع كل تويتة جديدة."})


def menu_action(cfg, state, handles, tags, action):
    admin = cfg["telegram_admin_id"]
    if action in ASK:
        state["awaiting"] = action
        tg_call(cfg, "sendMessage", {"chat_id": admin, "text": ASK[action],
                                     "reply_markup": {"force_reply": True, "input_field_placeholder": "اكتب هون..."}})
    elif action == "links":
        refresh_my_work(cfg, load_json(SETTINGS_FILE, {}), handles, state, full=True)
        for m in links_messages():
            tg_call(cfg, "sendMessage", {"chat_id": admin, "text": m, "parse_mode": "HTML",
                                         "disable_web_page_preview": True})
    elif action == "report":
        typing(cfg)
        tg_call(cfg, "sendMessage", {"chat_id": admin, "parse_mode": "HTML", "disable_web_page_preview": True,
                                     "reply_markup": REPORT_BUTTONS,
                                     "text": build_my_report(cfg, load_json(SETTINGS_FILE, {}), tags, 7)})
    else:
        if action == "leaderboard":
            typing(cfg)
        text = (build_leaderboard(cfg, tags, load_json(SETTINGS_FILE, {})) if action == "leaderboard"
                else handle_command(cfg, handles, tags, "/list"))
        tg_call(cfg, "sendMessage", {"chat_id": admin, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True, "reply_markup": MENU_KEYBOARD})


def process_commands(cfg, state, wait=0):
    """Read new Telegram messages and apply commands sent by the admin."""
    admin = str(cfg.get("telegram_admin_id") or "")
    if not admin:
        return
    token = cfg["telegram_bot_token"]
    offset = int(state.get("tg_offset") or 0)
    # wait > 0 = long polling: Telegram holds the request until a message arrives, so replies are instant
    data = http_get_json(f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout={wait}")
    handles = load_handles()
    tags = load_json(TAGS_FILE, {})
    before_handles, before_tags = list(handles), dict(tags)

    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        cb = upd.get("callback_query")
        if cb:
            data_ = cb.get("data") or ""
            if str((cb.get("from") or {}).get("id")) != admin:  # e.g. someone else in the channel
                try:
                    tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"],
                                                         "text": "Only the bot owner can use this button"})
                except Exception:
                    pass
            elif data_ == "noop":
                tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Done already ✓"})
            elif data_[:3] in ("lk:", "rt:"):
                try:
                    handle_engage_button(cfg, cb)
                except Exception as e:
                    print(f"[engage error] {e}")
            else:
                try:
                    if data_.startswith("rep:"):  # report period buttons
                        days = int(cb["data"][4:])
                        tg_call(cfg, "editMessageText", {
                            "chat_id": admin, "message_id": cb["message"]["message_id"], "parse_mode": "HTML",
                            "disable_web_page_preview": True, "reply_markup": REPORT_BUTTONS,
                            "text": build_my_report(cfg, load_json(SETTINGS_FILE, {}), tags, days)})
                        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"]})
                    else:
                        handle_draft_button(cfg, state, cb)
                except Exception as e:
                    print(f"[button error] {e}")
            continue
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text or str((msg.get("from") or {}).get("id")) != admin:
            continue
        try:
            replied_to = (msg.get("reply_to_message") or {}).get("message_id")
            first = text.split()[0].split("@")[0].lower()
            if text in MENU_ACTIONS:  # a menu button at the bottom of the chat
                state.pop("awaiting", None)
                menu_action(cfg, state, handles, tags, MENU_ACTIONS[text])
                continue
            if first in ("/start", "/menu", "/help"):
                show_menu(cfg)
                continue
            if text.lower() in POST_WORDS or first == "/post":  # "post" / "انشر"
                post_link(cfg, state, replied_to)
                continue
            draft_key = find_draft(state, replied_to)[0] if replied_to else None
            if draft_key and not text.startswith("/"):  # you replied to a draft with your idea
                rewrite_draft(cfg, state, draft_key, text)
                continue
            waiting = state.pop("awaiting", None)
            if waiting and not text.startswith("/"):  # answer to a button that asked you to type
                if waiting == "write":
                    new_tweet_draft(cfg, state, text)
                elif waiting in ("add", "remove"):
                    reply = handle_command(cfg, handles, tags, f"/{waiting} {text}")
                    tg_call(cfg, "sendMessage", {"chat_id": admin, "text": reply, "parse_mode": "HTML",
                                                 "reply_markup": MENU_KEYBOARD})
                else:
                    rewrite_draft(cfg, state, waiting, text)
                continue
            if first == "/links":
                menu_action(cfg, state, handles, tags, "links")
                continue
            report_days = {"/today": 1, "/week": 7, "/month": 30, "/all": 3650, "/mywork": 3650}
            if first in report_days or first == "/myreport":
                arg = text.partition(" ")[2].strip()
                days = report_days.get(first) or (int(arg) if arg.isdigit() else 7)
                tg_call(cfg, "sendMessage", {"chat_id": msg["chat"]["id"], "parse_mode": "HTML",
                                             "disable_web_page_preview": True, "reply_markup": REPORT_BUTTONS,
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
                       {"chat_id": msg["chat"]["id"], "text": reply, "parse_mode": "HTML",
                        "disable_web_page_preview": True})

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
    # Arabic translations are collapsed to one line (tap to expand) so the message stays short
    meaning = [arabic_quote(d["reply_ar"])] if d.get("reply_ar") else []
    if d.get("kind") == "tweet":
        return "\n".join([
            "📝 <b>تويتة جديدة</b>  ·  " + ("✅ <b>نشرتها</b>" if d.get("done") else "🟡 <b>لسا ما نشرتها</b>"),
            f"<i>🗒 {html.escape(d.get('instruction') or '')}</i>", LINE,
            "✍️ <b>التويتة</b> · اضغط عليها للنسخ", f"<code>{reply_txt}</code>"] + meaning)
    snippet = html.escape(strip_leading_mentions(clean_text(d["text"], drop_links=True))[:300]) or "📷 media post"
    status = "✅ <b>أنا رديت عليها</b>" if d.get("done") else "🟡 <b>لسا أنا ما رديت</b>"
    if d.get("to_me"):  # someone replied to you
        lines = [f"📩 <b>رد جديد عليك من @{html.escape(d['author'])}</b>", status, LINE]
        if d.get("my_text"):
            lines.append(f"<i>🗣 إنت كتبت:</i>\n<blockquote expandable>{html.escape(d['my_text'][:200])}</blockquote>")
        lines += ["<i>💬 هو رد:</i>", f"<blockquote>{snippet}</blockquote>"]
    else:
        lines = tier_header(tags, d["author"], d.get("followers")) + [f"🤖 <b>اقتراح رد</b>  ·  {status}", LINE,
                 f"{tag_line(tags, d['author'])}  ·  @{html.escape(d['author'])}",
                 f"<blockquote>{snippet}</blockquote>"]
    if d.get("tweet_ar"):
        lines.append(arabic_quote(d["tweet_ar"][:400]))
    lines += [LINE, "✍️ <b>الرد المقترح</b> · اضغط عليه للنسخ", f"<code>{reply_txt}</code>"] + meaning
    if d.get("instruction"):
        lines.append(f"<i>🗒 فكرتك: {html.escape(d['instruction'])}</i>")
    return "\n".join(lines)


def draft_buttons(tid, d):
    # twitter.com/intent/tweet is the link the X mobile app registers for, so it opens the app with the text ready
    # (x.com/intent/post only opens the website)
    is_tweet = d.get("kind") == "tweet"
    original = f"https://x.com/{d.get('author') or 'i'}/status/{tid}"
    if d.get("done"):  # already handled: link to your post (when we found it) and a way to undo
        first = ({"text": "🔗 افتح ردي على X", "url": d["my_url"]} if d.get("my_url")
                 else {"text": "👁 شوف التويتة", "url": original} if not is_tweet else None)
        rows = [[first]] if first else []
        return {"inline_keyboard": rows + [[{"text": "↩️ لسا ما نشرتها" if is_tweet else "↩️ لسا ما رديت",
                                             "callback_data": f"und:{tid}"}]]}
    if is_tweet:
        rows = [[{"text": "🚀 انشرها على X",
                  "url": "https://twitter.com/intent/tweet?" + urllib.parse.urlencode({"text": d["reply"]})}]]
    else:
        rows = [[{"text": "🚀 رد على X",
                  "url": "https://twitter.com/intent/tweet?" + urllib.parse.urlencode({"in_reply_to": tid, "text": d["reply"]})},
                 {"text": "👁 شوف التويتة", "url": original}]]
    if not is_tweet and is_volta_tweet(d):  # Volta's own posts and posts about Volta: like / repost too
        rows.append(engage_row(tid))
    return {"inline_keyboard": rows + [
        [{"text": "✏️ اكتب فكرتي", "callback_data": f"ins:{tid}"},
         {"text": "🔄 جيب غيرها", "callback_data": f"chg:{tid}"}],
        [{"text": "✅ نشرتها" if is_tweet else "✅ رديت خلص", "callback_data": f"don:{tid}"},
         {"text": "🗑 تجاهل", "callback_data": f"ign:{tid}"}],
    ]}


def apply_result(d, result):
    d["reply"] = result["reply"]
    d["relevant"] = result.get("relevant", d.get("relevant"))
    d["reply_ar"] = result.get("reply_ar") or ""
    d["tweet_ar"] = result.get("tweet_ar") or d.get("tweet_ar") or ""
    d["tried"] = (d.get("tried", []) + [d["reply"]])[-5:]


def regenerate(cfg, d, state, avoid=None):
    """Draft with the next angle in the rotation, and show the model recent drafts so posts don't repeat."""
    import ai_replies
    i = int(state.get("angle_i", 0))
    state["angle_i"] = i + 1
    angle = ai_replies.ANGLES[i % len(ai_replies.ANGLES)]
    recent = state.get("recent_texts", [])[-20:]
    if d.get("kind") == "tweet":
        result = ai_replies.write_tweet(cfg["anthropic_api_key"], d.get("instruction"), avoid=avoid,
                                        recent=recent, angle=angle)
    else:
        result = ai_replies.suggest_reply(cfg["anthropic_api_key"], d["author"], d["text"], avoid=avoid,
                                          instruction=d.get("instruction"), image=d.get("image"),
                                          recent=recent, angle=angle)
    if result and result.get("reply"):
        state["recent_texts"] = (state.get("recent_texts", []) + [result["reply"]])[-30:]
    return result


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


def typing(cfg):
    try:
        tg_call(cfg, "sendChatAction", {"chat_id": cfg["telegram_admin_id"], "action": "typing"})
    except Exception:
        pass


def rewrite_draft(cfg, state, key, instruction):
    """You gave your own idea for a draft (by replying to it or via the ✏️ button): rewrite it."""
    typing(cfg)
    d = state.get("drafts", {}).get(key)
    if not d:
        tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_admin_id"],
                                     "text": "That draft expired. Use /write your idea for a new tweet."})
        return
    d["instruction"] = instruction
    result = regenerate(cfg, d, state)
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
    typing(cfg)
    d = {"kind": "tweet", "instruction": instruction, "reply": "", "tried": []}
    result = regenerate(cfg, d, state)
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
         "image": tweet_media(tweet)[0], "followers": (tweet.get("author") or {}).get("followers")}
    result = regenerate(cfg, d, state)
    if not result or not result.get("reply"):
        return
    apply_result(d, result)
    tid = str(tweet.get("id"))
    show_draft(cfg, tags, tid, d)
    store_draft(state, tid, d)
    state["drafted_ids"] = (state.get("drafted_ids", []) + [tid])[-3000:]


def check_replies_to_me(cfg, settings, handles, state):
    """Someone replied to you on X: DM you their reply with a suggested answer and the usual buttons."""
    me = (settings.get("my_handle") or "").lstrip("@")
    if not me or not cfg.get("telegram_admin_id"):
        return
    since = int(state.get("last_to_me") or time.time() - 600)
    now = int(time.time())
    found = search_tweets(cfg["twitterapi_key"], f"to:{me} since_time:{since} -from:{me}")
    seen = set(state.get("to_me_seen", []))
    mine = load_json(MYWORK_FILE, {})
    tags = load_json(TAGS_FILE, {})
    for t in sorted(found, key=lambda x: int(x.get("id") or 0)):
        tid = str(t.get("id"))
        if tid in seen:
            continue
        seen.add(tid)
        author = (t.get("author") or {}).get("userName") or ""
        d = {"to_me": True, "author": author, "text": t.get("text") or "", "reply": "", "tried": [],
             "image": tweet_media(t)[0], "my_text": (mine.get(str(t.get("inReplyToId"))) or {}).get("text", "")}
        result = None
        if cfg.get("anthropic_api_key"):
            try:
                result = regenerate(cfg, d, state)
            except Exception as e:
                print(f"[reply-to-me draft error] {e}")
        if result and result.get("reply"):
            apply_result(d, result)
        else:
            d["reply"] = ""
        show_draft(cfg, tags, tid, d)
        store_draft(state, tid, d)
    state["to_me_seen"] = list(seen)[-1000:]
    state["last_to_me"] = now


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
                                 "text": f"🚀 <b>جاهزة للنشر</b>\n\n<code>{html.escape(d['reply'])}</code>\n\n"
                                         "<i>اضغط الزر، شيّك عليها، وبعدين Post على X.</i>",
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
    if action in ("don", "und"):  # ✅ رديت / undo
        d["done"] = action == "don"
        show_draft(cfg, load_json(TAGS_FILE, {}), key, d, edit_message_id=msg_id)
        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"],
                                             "text": "✅ تمام" if d["done"] else "رجعت 🟡"})
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
        tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "⏳ بكتب وحدة جديدة..."})
        typing(cfg)
        result = regenerate(cfg, d, state, avoid=d.get("tried"))
        if not result or not result.get("reply"):
            return
        apply_result(d, result)
        show_draft(cfg, load_json(TAGS_FILE, {}), key, d, edit_message_id=msg_id)


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


def account_tier(tags, handle, followers):
    """How loud an alert should be: 3 = Volta itself, 2 = official/huge accounts, 1 = notable, 0 = normal."""
    tag = (tags.get((handle or "").lower()) or "").lower()
    followers = followers if isinstance(followers, int) else 0
    if (handle or "").lower() == "voltafamily":
        return 3
    if "official" in tag or "founder" in tag or followers >= 100_000:
        return 2
    if "top kol" in tag or followers >= 25_000:
        return 1
    return 0


def tier_header(tags, handle, followers):
    tier = account_tier(tags, handle, followers)
    if tier == 3:
        return ["🚨🚨🚨 <b>VOLTA OFFICIAL</b> 🚨🚨🚨", "⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡"]
    if tier == 2:
        return [f"🔥🔥 <b>BIG ACCOUNT</b> · {short_num(followers)} followers 🔥🔥"]
    if tier == 1:
        return [f"⭐ <b>NOTABLE</b> · {short_num(followers)} followers"]
    return []


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

    top = tier_header(tags, handle, author.get("followers"))
    if mention:
        top.append("💬 <b>$VOLTA MENTION</b>")
    elif is_highlight(tweet, settings):
        top.append("⭐ <b>IMPORTANT</b> ⭐")
    top.append(tag_line(tags, handle))

    parts = top + [LINE,
                   f"👤 <b>{name}</b>  @{html.escape(handle)}",
                   f"👥 {short_num(author.get('followers'))} followers",
                   "",
                   f"<blockquote>{text}</blockquote>",
                   LINE]
    tags_txt = " ".join(x for x in (tag_hashtag(tags, handle), "#VoltaRadar") if x)
    parts.append(f"⚡ {tags_txt}")
    return "\n".join(parts)


X_READY = False  # set in main() when the X API keys are present


def engage_row(tweet_id):
    """❤️ / 🔁 buttons. With X API keys they like / repost in one tap from Telegram;
    without them they open X's like / repost screen."""
    if X_READY:
        return [{"text": "❤️ Like", "callback_data": f"lk:{tweet_id}"},
                {"text": "🔁 Repost", "callback_data": f"rt:{tweet_id}"}]
    return [{"text": "❤️ Like", "url": f"https://twitter.com/intent/like?tweet_id={tweet_id}"},
            {"text": "🔁 Repost", "url": f"https://twitter.com/intent/retweet?tweet_id={tweet_id}"}]


def handle_engage_button(cfg, cb):
    """❤️ / 🔁 pressed: like or repost on X right away, then mark the button done."""
    import x_actions
    action, _, tweet_id = cb["data"].partition(":")
    done_text = "❤️ Liked ✓" if action == "lk" else "🔁 Reposted ✓"
    try:
        (x_actions.like if action == "lk" else x_actions.repost)(cfg, tweet_id)
    except Exception as e:
        if "already" not in str(e).lower():  # "already retweeted" just means it's done
            tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "show_alert": True,
                                                 "text": f"ما زبط ❌\n{str(e)[:150]}"})
            return
        done_text += " (already)"
    tg_call(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": done_text})
    msg = cb.get("message") or {}
    keyboard = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    for row in keyboard:
        for b in row:
            if b.get("callback_data") == cb["data"]:
                b.clear()
                b.update({"text": done_text, "callback_data": "noop"})
    try:
        tg_call(cfg, "editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                                "reply_markup": {"inline_keyboard": keyboard}})
    except Exception as e:
        print(f"[engage edit error] {e}")


def is_volta_tweet(tweet_or_draft):
    """From @VoltaFamily, or anyone posting / replying about Volta."""
    author = (tweet_or_draft.get("author") or {})
    handle = author.get("userName") if isinstance(author, dict) else author
    text = (tweet_or_draft.get("text") or "").lower()
    return (handle or "").lower() == "voltafamily" or "volta" in text


def send_telegram(token, chat_id, text, url=None, profile=None, photo=None, engage_id=None):
    payload = {"chat_id": chat_id, "parse_mode": "HTML"}
    buttons = []
    if url:
        buttons.append({"text": "🔗 Open on X", "url": url})
    if profile:
        buttons.append({"text": "👤 Profile", "url": f"https://x.com/{profile}"})
    rows = ([engage_row(engage_id)] if engage_id else []) + ([buttons] if buttons else [])
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}
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


SPAM_WORDS = ("collab", "dm me", "check dm", "let's connect", "lets connect", "let's talk", "partnership",
              "reach out", "inbox", "work together", "free of charge", "at no cost", "follow back", "hit me up",
              "send a message", "message me", "let's work", "promote", "hype your project")


def is_spam(text):
    low = (text or "").lower()
    return any(w in low for w in SPAM_WORDS)


def build_leaderboard(cfg, tags, settings):
    """Who talks about Volta the most, and the strongest Volta posts, counted live from X (Volta only)."""
    try:
        start = calendar.timegm(time.strptime(settings.get("work_start", ""), "%Y-%m-%d"))
    except ValueError:
        start = int(time.time()) - 7 * 86400
    terms = settings.get("mention_terms") or ["$VOLTA", "@VoltaFamily"]
    query = "(" + " OR ".join(terms + ["volta.family"]) + f") since_time:{start} -filter:retweets -from:VoltaFamily"
    tweets = [t for t in search_tweets(cfg["twitterapi_key"], query)
              if not is_spam(t.get("text"))  # drop "let's collab / DM me" promo accounts
              and int((t.get("author") or {}).get("followers") or 0) >= 100]

    counts, followers = {}, {}
    for t in tweets:
        a = t.get("author") or {}
        h = a.get("userName")
        if h:
            counts[h] = counts.get(h, 0) + 1
            followers[h] = a.get("followers")

    def score(t):
        return int(t.get("likeCount") or 0) + 3 * int(t.get("retweetCount") or 0) + 2 * int(t.get("replyCount") or 0)

    lines = ["🏆 <b>VOLTA LEADERBOARD</b>",
             f"<i>Volta talk only · since {time.strftime('%b %d', time.gmtime(start))}</i>", "",
             "💬 <b>TOP VOLTA VOICES</b>"]
    top = sorted(counts.items(), key=lambda kv: (-kv[1], -(followers.get(kv[0]) or 0)))[:5]
    lines += [f"{medal} @{html.escape(h)}  ·  {c} post{'s' if c != 1 else ''}  ·  👥 {short_num(followers.get(h))}"
              for medal, (h, c) in zip(MEDALS, top)] or ["<i>Nothing yet.</i>"]

    best = sorted(tweets, key=score, reverse=True)[:5]
    lines += ["", "🔥 <b>TOP VOLTA POSTS</b>"]
    lines += [f"{medal} <a href=\"{tweet_url(t)}\">{html.escape((t.get('author') or {}).get('userName') or '?')}</a>"
              f"  ·  ❤️ {short_num(int(t.get('likeCount') or 0))}  🔁 {short_num(int(t.get('retweetCount') or 0))}"
              f"  👁 {short_num(int(t.get('viewCount') or 0))}"
              for medal, t in zip(MEDALS, best)] or ["<i>Nothing yet.</i>"]
    lines += ["", f"<i>{len(tweets)} Volta posts from {len(counts)} accounts</i>", "⚡ #Leaderboard #VoltaRadar"]
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


def split_lanes(handles, tags):
    """Fast lane: Volta + our ecosystem (checked every few seconds). Slow lane: everyone else."""
    fast = [h for h in handles if h.lower() == "voltafamily" or tag_marker(tags.get(h.lower())) == "🟢"]
    slow = [h for h in handles if h not in fast]
    return fast, slow


def check_once(cfg, settings, handles, state):
    # Credits are spent per search, so each group is checked at its own speed:
    #   fast lane (Volta + ecosystem) every fast_seconds, everyone else every slow_seconds, mentions every mention_seconds.
    # X search shows new tweets a minute or two late, so each search looks back a little past its last run;
    # tweets we already sent are skipped by their id.
    lookback = int(settings.get("lookback_seconds", 180))
    now = int(time.time())
    seen = set(state.get("seen_ids", []))
    new_tweets = []  # (tweet, is_mention)
    failed = False
    fast, slow = split_lanes(handles, load_json(TAGS_FILE, {}))
    lanes = [("fast", fast, int(settings.get("fast_seconds", 30))),
             ("slow", slow, int(settings.get("slow_seconds", 180))),
             ("mentions", handles, int(settings.get("mention_seconds", 90)))]

    for lane, members, every in lanes:
        last = int(state.get(f"last_{lane}") or state.get("last_check") or now - 900)
        if now - last < every or not members:
            continue
        since = last - lookback
        lane_ok = True
        if lane == "mentions":
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
                        if is_spam(t.get("text")):  # "love the vision, DM me for collab" promo bots
                            continue
                        new_tweets.append((t, True))
                except Exception as e:
                    print(f"[mention search error] {e}")
                    lane_ok = False
        else:
            for i in range(0, len(members), BATCH_SIZE):
                batch = members[i:i + BATCH_SIZE]
                query = "(" + " OR ".join(f"from:{h}" for h in batch) + f") since_time:{since}"
                if not cfg.get("include_replies", False):
                    query += " -filter:replies"
                try:
                    new_tweets.extend((t, False) for t in search_tweets(cfg["twitterapi_key"], query))
                except Exception as e:  # keep going with the other batches
                    print(f"[search error] {e}")
                    lane_ok = False
        if lane_ok:  # on error, keep the old window so the next run catches up
            state[f"last_{lane}"] = now
        failed = failed or not lane_ok

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
                          (t.get("author") or {}).get("userName"), tweet_media(t)[0],
                          engage_id=str(t.get("id")) if (mention or is_volta_tweet(t)) else None)
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

    if not failed:
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
    send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], build_leaderboard(cfg, tags, settings))
    if cfg.get("telegram_admin_id") and settings.get("my_handle"):  # your daily work log, DM only
        try:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_admin_id"],
                          build_my_report(cfg, settings, tags, 1))
        except Exception as e:
            print(f"[my report error] {e}")
    state["last_digest"] = today
    save_json(STATE_FILE, state)
    print("daily report sent")


LIVE_STATE = {}  # the running bot's state, so reports can see which tweets the bot drafted


def is_via_bot(t):
    """Your tweet answers a tweet the bot drafted a reply for, or matches one of its drafts."""
    if str(t.get("inReplyToId") or "") in set(LIVE_STATE.get("drafted_ids", [])):
        return True
    text = " ".join(w for w in (t.get("text") or "").lower().split() if not w.startswith("@"))[:50]
    return bool(text) and any(r.lower()[:50] == text for r in LIVE_STATE.get("recent_texts", []))


def mark_answered_drafts(cfg, tags, my_tweets):
    """Your reply showed up on X: turn that draft green (✅ تم الرد) with a link to your reply."""
    drafts = LIVE_STATE.get("drafts", {})
    for t in my_tweets:
        d = drafts.get(str(t.get("inReplyToId") or ""))
        if not d or (d.get("done") and d.get("my_url")):
            continue
        d["done"], d["my_url"] = True, tweet_url(t)
        if d.get("message_id") and cfg.get("telegram_admin_id"):
            try:
                show_draft(cfg, tags, str(t["inReplyToId"]), d, edit_message_id=d["message_id"])
            except Exception as e:
                print(f"[mark done error] {e}")


def fetch_my_volta_tweets(cfg, me, tags, since):
    """Your Volta-related tweets since a time; every one is also saved to mywork.json with its link."""
    tweets = search_tweets(cfg["twitterapi_key"], f"from:{me} since_time:{since} -filter:retweets")

    def about_volta(t):
        to = (t.get("inReplyToUsername") or "").lower()
        return ("volta" in (t.get("text") or "").lower() or to == "voltafamily"
                or tag_marker(tags.get(to)) == "🟢" or is_via_bot(t))

    tweets = [t for t in tweets if about_volta(t)]
    mark_answered_drafts(cfg, tags, tweets)
    saved = load_json(MYWORK_FILE, {})
    for t in tweets:
        saved[str(t.get("id"))] = {
            "url": tweet_url(t), "to": t.get("inReplyToUsername") or "", "date": t.get("createdAt", ""),
            "text": (t.get("text") or "")[:280], "views": int(t.get("viewCount") or 0),
            "likes": int(t.get("likeCount") or 0), "via_bot": is_via_bot(t)}
    save_json(MYWORK_FILE, saved)
    return tweets


def refresh_my_work(cfg, settings, handles, state, full=False):
    """Keep mywork.json up to date with your Volta tweets. The regular pass only reads the last 2 hours
    (enough to turn answered drafts green); full=True re-reads everything since work_start."""
    me = (settings.get("my_handle") or "").lstrip("@")
    if not me:
        return
    try:
        since = calendar.timegm(time.strptime(settings.get("work_start", ""), "%Y-%m-%d"))
    except ValueError:
        since = int(time.time()) - 7 * 86400
    if not full:
        since = max(since, int(time.time()) - 2 * 3600)
    fetch_my_volta_tweets(cfg, me, load_json(TAGS_FILE, {}), since)


def links_messages():
    """All saved Volta tweet links, newest first, split into Telegram-sized messages."""
    saved = sorted(load_json(MYWORK_FILE, {}).items(), key=lambda kv: int(kv[0]), reverse=True)
    if not saved:
        return ["No saved links yet."]
    header = f"🔗 <b>MY VOLTA LINKS</b> · {len(saved)} total (tap to copy)\n{LINE}"
    msgs, cur = [], header
    for i, (_, v) in enumerate(saved, 1):
        kind = f"↪️ @{html.escape(v['to'])}" if v.get("to") else "📝 Post"
        line = f"\n<b>{i}.</b> {kind} · 👁 {short_num(v.get('views'))}\n<code>{v['url']}</code>"
        if len(cur) + len(line) > 3900:
            msgs.append(cur)
            cur = ""
        cur += line
    msgs.append(cur)
    return msgs


def build_my_report(cfg, settings, tags, days):
    """Your own X activity: replies and posts with likes/views, to show the work you've done."""
    me = (settings.get("my_handle") or "").lstrip("@")
    if not me:
        return "Set <code>my_handle</code> in settings.json first."
    since = int(time.time()) - days * 86400
    try:  # never count anything from before the Volta work started
        start = calendar.timegm(time.strptime(settings.get("work_start", ""), "%Y-%m-%d"))
        if start > since:
            since, days = start, 0  # 0 = "since the work started"
    except ValueError:
        pass
    tweets = fetch_my_volta_tweets(cfg, me, tags, since)
    via_bot = sum(1 for t in tweets if is_via_bot(t))
    is_reply = lambda t: bool(t.get("isReply") or t.get("inReplyToUsername"))
    replies = [t for t in tweets if is_reply(t)]
    posts = [t for t in tweets if not is_reply(t)]

    def n(t, k):
        return int(t.get(k) or 0)

    total = {k: sum(n(t, k) for t in tweets) for k in ("likeCount", "viewCount", "retweetCount", "replyCount")}
    volta = sum(1 for t in tweets if "volta" in (t.get("text") or "").lower())
    avg_views = total["viewCount"] // len(tweets) if tweets else 0
    engagement = total["likeCount"] + total["retweetCount"] + total["replyCount"]
    rate = f"{engagement / total['viewCount'] * 100:.1f}%" if total["viewCount"] else "0%"
    today = time.strftime("%b %d", time.gmtime())
    if days == 1:
        period = f"Today · {today}"
    else:
        label = "Since start" if days == 0 else f"Last {days} days"
        period = f"{label} · {time.strftime('%b %d', time.gmtime(since))} – {today}"

    def when(t):
        try:
            return time.strftime("%b %d", time.strptime(t.get("createdAt", ""), "%a %b %d %H:%M:%S %z %Y"))
        except ValueError:
            return ""

    def row(label, value):  # fixed-width row for the stats table
        return f"{label:<16}{value:>8}"

    # key numbers as an aligned monospace table, so nothing runs together
    table = "\n".join([
        row("Total activity", len(tweets)),
        row("  Replies", len(replies)),
        row("  Posts", len(posts)),
        row("Mention $VOLTA", volta),
        row("Via Volta Radar", via_bot),
        "",
        row("Impressions", short_num(total["viewCount"])),
        row("Avg per tweet", short_num(avg_views)),
        row("Likes", short_num(total["likeCount"])),
        row("Reposts", short_num(total["retweetCount"])),
        row("Comments", short_num(total["replyCount"])),
        row("Engagement rate", rate),
    ])
    lines = ["📊 <b>VOLTA CONTRIBUTION REPORT</b>",
             f"<i>@{html.escape(me)} · {period}</i>",
             "",
             f"<pre>{html.escape(table)}</pre>"]

    by_cat = {}
    for t in replies:
        marker = tag_marker(tags.get((t.get("inReplyToUsername") or "").lower()))
        by_cat[marker] = by_cat.get(marker, 0) + 1
    if by_cat:
        names = {"🟢": "Ecosystem", "🔵": "Networks", "🟣": "KOLs", "⚪": "Community"}
        lines += ["", "🎯 <b>AUDIENCE</b>",
                  "   ".join(f"{m} {names[m]} <b>{c}</b>" for m, c in sorted(by_cat.items(), key=lambda kv: -kv[1]))]

    top = sorted(tweets, key=lambda t: (n(t, "viewCount"), n(t, "likeCount")), reverse=True)[:3]
    if top:
        lines += ["", "🏆 <b>TOP PERFORMING</b>"]
        for medal, t in zip(MEDALS, top):
            to = t.get("inReplyToUsername")
            where = f"↪️ {html.escape(to)}" if to else "📝 Post"
            lines.append(f"{medal} <a href=\"{tweet_url(t)}\">{where}</a>  ·  👁 {short_num(n(t, 'viewCount'))}"
                         f"  ❤️ {n(t, 'likeCount')}")

    if tweets:
        newest = sorted(tweets, key=lambda t: int(t.get("id") or 0), reverse=True)
        shown = newest[:30]  # Telegram messages are capped at 4096 characters
        more = f" · latest {len(shown)} of {len(tweets)}" if len(shown) < len(tweets) else ""
        lines += ["", f"📜 <b>ACTIVITY LOG</b>{more}"]
        for i, t in enumerate(shown, 1):
            to = t.get("inReplyToUsername")
            # the handle is plain text inside the link (no "@"), so the whole line opens your tweet
            kind = f"↪️ {html.escape(to)}" if to else "📝 Post"
            lines.append(f"<code>{i:>2}.</code> <a href=\"{tweet_url(t)}\">{kind}</a>  ·  {when(t)}"
                         f"  ·  👁 {short_num(n(t, 'viewCount'))}  ❤️ {n(t, 'likeCount')}")
    else:
        lines += ["", "<i>No activity in this period.</i>"]
    lines += ["", "<i>Generated by Volta Radar</i>"]
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990].rsplit("\n", 1)[0] + "\n…"


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
             f"👤 <b>{html.escape(u.get('name') or user)}</b>  @{user}",
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


def git_sync():
    """On GitHub Actions: commit the bot's state files and push, so the next run continues from here."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    files = [str(p.name) for p in (STATE_FILE, HANDLES_FILE, TAGS_FILE, STATS_FILE, MYWORK_FILE) if p.exists()]
    with STATE_LOCK:
        subprocess.run(["git", "add", *files], cwd=ROOT)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", "Update radar state"], cwd=ROOT)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs"], cwd=ROOT)


def command_loop(cfg, state, stop):
    """Answers your buttons and messages within a second or two (long polling), separate from the checks."""
    while not stop.is_set():
        try:
            process_commands(cfg, state, wait=25)
        except Exception as e:
            print(f"[commands error] {e}")
            time.sleep(3)


def run_daemon(cfg, state, minutes):
    """One long-running process: instant command thread + tweet checks every few seconds."""
    stop = threading.Event()
    threading.Thread(target=command_loop, args=(cfg, state, stop), daemon=True).start()
    end = time.time() + minutes * 60
    last_sync = last_extras = last_mine = 0
    while time.time() < end:
        started = time.time()
        settings = load_json(SETTINGS_FILE, {})
        handles = load_handles()
        try:
            check_once(cfg, settings, handles, state)
        except Exception as e:
            print(f"[check error] {e}")
        if time.time() - last_mine > int(settings.get("mine_seconds", 120)):  # your tweets + replies to you
            last_mine = time.time()
            for mine in (check_replies_to_me, refresh_my_work):
                try:
                    mine(cfg, settings, handles, state)
                except Exception as e:
                    print(f"[{mine.__name__} error] {e}")
        if time.time() - last_extras > 300:  # digest / follows: every 5 minutes is plenty
            last_extras = time.time()
            for extra in (daily_report, check_new_follows):
                try:
                    extra(cfg, settings, handles, state)
                except Exception as e:
                    print(f"[{extra.__name__} error] {e}")
        if time.time() - last_sync > 120:
            last_sync = time.time()
            git_sync()
        pause = float(settings.get("check_seconds", 30)) - (time.time() - started)
        if pause > 0:
            time.sleep(pause)
    stop.set()
    save_json(STATE_FILE, state)
    git_sync()


def main():
    global LIVE_STATE
    cfg = load_json(CONFIG_FILE, {})
    # on GitHub Actions the keys come from repository secrets instead of config.json
    for key, env in [("twitterapi_key", "TWITTERAPI_KEY"),
                     ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
                     ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
                     ("telegram_admin_id", "TELEGRAM_ADMIN_ID"),
                     ("anthropic_api_key", "ANTHROPIC_API_KEY"),
                     ("x_api_key", "X_API_KEY"), ("x_api_secret", "X_API_SECRET"),
                     ("x_access_token", "X_ACCESS_TOKEN"), ("x_access_secret", "X_ACCESS_SECRET")]:
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if not cfg.get("twitterapi_key") or not cfg.get("telegram_bot_token"):
        sys.exit("No keys found. Fill in config.json or set the environment variables.")

    if "--chat-id" in sys.argv:
        print_chat_ids(cfg["telegram_bot_token"])
        return

    global X_READY
    import x_actions
    X_READY = x_actions.configured(cfg)

    state = LIVE_STATE = load_json(STATE_FILE, {})
    if "--daemon" in sys.argv:
        minutes = float(sys.argv[sys.argv.index("--daemon") + 1])
        run_daemon(cfg, state, minutes)
        return
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
