"""
AI writing help: reply ideas for tweets and custom tweets, drafted by Claude.
Drafts go to the admin's private chat only, and nothing is posted on X automatically.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"

FACTS = """About Volta (only use these facts; they come from @VoltaFamily's own posts):
- Volta is "the Elysium launchpad for next-gen tokens". Elysium is the network built by Kinetiq, accelerating Hyperliquid.
- Volta is the first Uniswap V4 hook live on Elysium. Every token launched gets its own Uniswap V4 pool paired with HYPE.
- Built into the hook: creator-set fees, anti-snipe protection at launch, and buyback & burn on every trade.
- Creators earn fees in HYPE and tokenized stocks.
- Like the Pons model, 80% of trading fees buy back $VOLTA and every token bought back is burned
  (there is a live burn dashboard).
- Product: charts that show your own buys/sells and average entry, a trending board, a free fast Elysium RPC,
  bridging to Elysium from 10+ chains. Volta launches already trade on BasedBot, JustRektTool and Agora.
- Volta is already live; Elysium's public mainnet launch is November 9.
- X account: @VoltaFamily

Do NOT say or imply: that the hook is approved, licensed or "official" Uniswap (it has only been submitted to
Uniswap for approval), that other launchpads are forks or unauthorized, or that Volta will be listed on any app
or aggregator in the future."""

# Each draft leans on one of these, rotating so posts don't all sound the same.
ANGLES = [
    "creators get paid: launch a token and you earn fees in HYPE",
    "creators can also earn in tokenized stocks, not just crypto",
    "buyback and burn: 80% of fees buy back $VOLTA and burn it, more trades means less supply",
    "tech: first uniswap v4 hook live on elysium, every token gets its own v4 pool paired with HYPE",
    "fair launches: anti-snipe protection at launch so bots don't eat the first block",
    "product: charts that show your own trades and average entry, or the trending board",
    "elysium: building where the hyperliquid ecosystem is heading, public mainnet nov 9",
    "what volta shipped recently, or a question for launchers about what a good launch looks like",
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
- the user is NOT on the Volta team. they are a $VOLTA holder and community supporter. never write "we built",
  "we shipped", "our launchpad", "at volta we" or anything that claims to be the team. speak as a user and holder:
  "volta lets creators...", "been watching volta...", "what i like about volta...". saying "i hold $VOLTA" when it
  fits is good, it keeps things honest
- always include the ticker $VOLTA exactly once, worked naturally into the sentence (not tacked on at the end).
  $VOLTA is the token (the one that gets bought back and burned); the platform is "volta". never write
  "we built $VOLTA" or call the launchpad $VOLTA
- describe what the product does, never promise money: no "fund your bag", "get paid big", "easy money",
  "passive income", or anything that implies profit
- never tell anyone to buy, ape, or invest; no price talk, no "100x", no financial advice
- no claims about competitors, not even implied ones (don't suggest other launchpads are forks, unsafe or
  "mystery code"), and nothing about Volta beyond the facts above"""

REPLY_SYSTEM = f"""You help a $VOLTA holder and community supporter write replies on X (Twitter).

{FACTS}

For the tweet you are given, write one short reply (ideally under 140 characters, max 180) that promotes Volta well.
- Start from what the tweet actually says, then connect it to Volta in one smooth step. It should read like
  something an informed holder would naturally say in the conversation, not an ad dropped under someone's post.
- Use one Volta angle only. Prefer the suggested angle if it fits the tweet; otherwise pick a better one.
- set relevant to true when the tweet is about launchpads, launching tokens, Elysium, Kinetiq, Hyperliquid
  builders, Uniswap V4 or creator fees (the link is natural); false when you had to bridge from an unrelated topic.
  When it's a bridge, keep it light and playful, a quick nod to volta rather than a pitch.
- If the user gives an instruction, follow it (tone, points to make), still within the limits below.
- tweet_ar: a natural Arabic translation of the original tweet's message, so an Arabic speaker understands it.
  leave out the @handles at the start and any links; translate only what the person actually said.
- reply_ar: a short Arabic translation of your reply, so the user knows what it says.

{RULES}"""

TWEET_SYSTEM = f"""You help a $VOLTA holder and community supporter write their own tweets on X (Twitter).

{FACTS}

Write one tweet that promotes Volta well (ideally 150-230 characters, max 260).
Follow the user's instruction if there is one; otherwise build it around the suggested angle.
Explain that one feature clearly enough that someone new gets what it is and why it matters,
in plain words a degen would use. Sound like a confident early user who gets it, not an ad.
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
