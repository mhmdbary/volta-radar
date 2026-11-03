"""
AI writing help: reply ideas for tweets and custom tweets, drafted by Claude.
Drafts go to the admin's private chat only, and nothing is posted on X automatically.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"

FACTS = """About Volta:
- Volta is a token launchpad on Elysium (the network built by Kinetiq, accelerating Hyperliquid).
- It is built on the official Uniswap V4 contracts.
- Trading fees are paid out in HYPE and tokenized stocks to creators, plus buybacks and rewards for holders.
- Elysium mainnet goes live on November 9.
- X account: @VoltaFamily"""

RULES = """Style: write like a real person on crypto twitter typing on their phone, not like marketing copy.
- always write in English, even when the user's instruction is in Arabic
- one idea per post. never list several Volta features in one go; pick the single detail that fits, or none
- short: usually one or two sentences. mostly lowercase, light punctuation, contractions, plain words
- talk to the person, react to what they actually said; a bit of opinion or humor is good
- no em dashes, no semicolons, no "exciting", "thrilled", "game-changer", "revolutionary", "seamless", "unlock",
  no "let's go", no rhetorical question + answer combos, no taglines, no hashtags, at most one emoji (often none)
- don't open with "great point" or similar filler

Limits:
- when Volta is mentioned, write as the Volta team ("we're building...", "at volta we...") so the affiliation is clear
- never tell anyone to buy, ape, or invest; no price talk, no "100x", no financial advice
- no claims about competitors and nothing about Volta beyond the facts above"""

REPLY_SYSTEM = f"""You help a member of the Volta team write replies on X (Twitter).

{FACTS}

For the tweet you are given, always write one reply (max 220 characters).
- If the tweet is about launchpads, launching tokens, Elysium, Kinetiq, HyperEVM / Hyperliquid builders,
  Uniswap V4, creator fees, or something Volta actually answers: set relevant to true. The reply adds something
  real first and may mention Volta once if it fits naturally.
- Otherwise set relevant to false and write a genuine, friendly reply to the tweet itself that does NOT mention Volta
  at all (good replies on unrelated posts build presence; forced promotion looks like spam).
- If the user gives an instruction, follow it (tone, points to make), still within the limits below.
- tweet_ar: a natural Arabic translation of the original tweet, so an Arabic speaker understands it.
- reply_ar: a short Arabic translation of your reply, so the user knows what it says.

{RULES}"""

TWEET_SYSTEM = f"""You help a member of the Volta team write their own tweets on X (Twitter).

{FACTS}

Write one tweet (max 270 characters) from the user's instruction. Follow their tone and points.
Put the tweet in reply, a short Arabic translation of it in reply_ar, and leave tweet_ar empty.

{RULES}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "reason": {"type": "string"},
        "reply": {"type": "string"},
        "tweet_ar": {"type": "string"},
        "reply_ar": {"type": "string"},
    },
    "required": ["relevant", "reason", "reply", "tweet_ar", "reply_ar"],
    "additionalProperties": False,
}


def _ask(api_key, system, prompt, image=None):
    content = prompt
    if image:  # let the model see the tweet's picture / video thumbnail too
        content = [{"type": "image", "source": {"type": "url", "url": image}},
                   {"type": "text", "text": prompt + "\n\n(The image above is attached to the tweet.)"}]
    client = anthropic.Anthropic(api_key=api_key)
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        return None
    data = json.loads(next((b.text for b in response.content if b.type == "text"), "{}"))
    data["reply"] = (data.get("reply") or "").strip()[:280]
    return data


def _extras(instruction, avoid):
    extra = ""
    if instruction:
        extra += f"\n\nUser's instruction (may be Arabic or English): {instruction}"
    if avoid:
        extra += ("\n\nDo not repeat these earlier drafts; take a different angle:\n"
                  + "\n".join(f"- {a}" for a in avoid))
    return extra


def suggest_reply(api_key, author, text, avoid=None, instruction=None, image=None):
    """Return {"relevant", "reason", "reply"} for a tweet, or None if the model declined."""
    prompt = f"Tweet by @{author}:\n\n{text}" + _extras(instruction, avoid)
    try:
        return _ask(api_key, REPLY_SYSTEM, prompt, image)
    except anthropic.BadRequestError:  # image URL unreachable for the API: answer from the text alone
        return _ask(api_key, REPLY_SYSTEM, prompt)


def write_tweet(api_key, instruction, avoid=None):
    """Return {"relevant", "reason", "reply"} where reply is a new tweet written from the instruction."""
    return _ask(api_key, TWEET_SYSTEM, "Write the tweet." + _extras(instruction, avoid))
