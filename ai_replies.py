"""
AI writing help: reply ideas for tweets and custom tweets, drafted by Claude.
Drafts go to the admin's private chat only, and nothing is posted on X automatically.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"

FACTS = """About Volta (only use these facts):
- Volta is a token launchpad on Elysium (the network built by Kinetiq, accelerating Hyperliquid).
- It is built on the official Uniswap V4 contracts.
- Creators earn the trading fees of the tokens they launch, paid in HYPE and tokenized stocks.
- Holders: 80% of fees go to buying back $VOLTA, and every token bought back is burned.
- Elysium mainnet goes live on November 9, and Volta is live on it from day one.
- X account: @VoltaFamily"""

# Each draft leans on one of these, rotating so posts don't all sound the same.
ANGLES = [
    "creators get paid: launch a token and you earn its trading fees in HYPE",
    "creators can also earn in tokenized stocks, not just crypto",
    "buyback and burn: fees buy back $VOLTA and burn it, so activity shrinks supply",
    "built on the official uniswap v4 contracts, battle-tested infrastructure",
    "home on elysium: launching where the hyperliquid ecosystem is heading",
    "day one: volta is live when elysium mainnet goes live nov 9",
    "builder life: what the team is shipping or thinking about right now",
    "a question or observation for launchers and degens about how launches should work",
]

RULES = """Style: write like a real person on crypto twitter typing on their phone, not like marketing copy.
- always write in English, even when the user's instruction is in Arabic
- one idea per post. never list several Volta features in one go
- short: usually one or two sentences. mostly lowercase, light punctuation, contractions, plain words
- talk to the person, react to what they actually said; a bit of opinion or humor is good
- vary the shape: sometimes a reaction, sometimes a question, sometimes a one-liner, sometimes a quick take
- no em dashes, no semicolons, no "exciting", "thrilled", "game-changer", "revolutionary", "seamless", "unlock",
  no "let's go", no rhetorical question + answer combos, no taglines, no hashtags, at most one emoji (often none)
- don't open with "great point" or similar filler

Limits:
- write as the Volta team ("we're building...", "at volta we...") so the affiliation is clear
- never tell anyone to buy, ape, or invest; no price talk, no "100x", no financial advice
- no claims about competitors, not even implied ones (don't suggest other launchpads are forks, unsafe or
  "mystery code"), and nothing about Volta beyond the facts above"""

REPLY_SYSTEM = f"""You help a member of the Volta team write replies on X (Twitter).

{FACTS}

For the tweet you are given, write one short reply (ideally under 140 characters, max 180) that promotes Volta well.
- Start from what the tweet actually says, then connect it to Volta in one smooth step. It should read like
  something a builder would naturally say in the conversation, not an ad dropped under someone's post.
- Use one Volta angle only. Prefer the suggested angle if it fits the tweet; otherwise pick a better one.
- set relevant to true when the tweet is about launchpads, launching tokens, Elysium, Kinetiq, Hyperliquid
  builders, Uniswap V4 or creator fees (the link is natural); false when you had to bridge from an unrelated topic.
  When it's a bridge, keep it light and playful, a quick nod to volta rather than a pitch.
- If the user gives an instruction, follow it (tone, points to make), still within the limits below.
- tweet_ar: a natural Arabic translation of the original tweet, so an Arabic speaker understands it.
- reply_ar: a short Arabic translation of your reply, so the user knows what it says.

{RULES}"""

TWEET_SYSTEM = f"""You help a member of the Volta team write their own tweets on X (Twitter).

{FACTS}

Write one short tweet that promotes Volta well (ideally under 160 characters, max 220).
Follow the user's instruction if there is one; otherwise build it around the suggested angle.
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


def _extras(instruction, avoid, recent, angle):
    extra = ""
    if angle:
        extra += f"\n\nSuggested angle: {angle}"
    if instruction:
        extra += f"\n\nUser's instruction (may be Arabic or English): {instruction}"
    if avoid:
        extra += ("\n\nThe user wants a different version. Do not repeat these drafts; change the angle and wording:\n"
                  + "\n".join(f"- {a}" for a in avoid))
    if recent:
        extra += ("\n\nRecent posts already written for this account. Yours must not sound like any of them: "
                  "different opening, different angle, different phrasing, no reused phrases:\n"
                  + "\n".join(f"- {r}" for r in recent))
    return extra


def suggest_reply(api_key, author, text, avoid=None, instruction=None, image=None, recent=None, angle=None):
    """Return {"relevant", "reason", "reply", ...} for a tweet, or None if the model declined."""
    prompt = f"Tweet by @{author}:\n\n{text}" + _extras(instruction, avoid, recent, angle)
    try:
        return _ask(api_key, REPLY_SYSTEM, prompt, image)
    except anthropic.BadRequestError:  # image URL unreachable for the API: answer from the text alone
        return _ask(api_key, REPLY_SYSTEM, prompt)


def write_tweet(api_key, instruction, avoid=None, recent=None, angle=None):
    """Return {"relevant", "reason", "reply", ...} where reply is a new tweet written from the instruction."""
    return _ask(api_key, TWEET_SYSTEM, "Write the tweet." + _extras(instruction, avoid, recent, angle))
