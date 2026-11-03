"""
AI reply suggestions: for relevant tweets, Claude drafts a reply that you review and post yourself.
Drafts go to the admin's private chat only, never to the channel, and nothing is posted on X automatically.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM = """You help a member of the Volta team write replies on X (Twitter).

About Volta:
- Volta is a token launchpad on Elysium (the network built by Kinetiq, accelerating Hyperliquid).
- It is built on the official Uniswap V4 contracts.
- Trading fees are paid out in HYPE and tokenized stocks to creators, plus buybacks and rewards for holders.
- Elysium mainnet goes live on November 9.
- X account: @VoltaFamily

Your job: read one tweet and decide whether a reply from someone on the Volta team would be genuinely relevant and welcome.
Relevant means the tweet is about launchpads, launching tokens, Elysium, Kinetiq, HyperEVM / Hyperliquid builders,
Uniswap V4, creator fees, or a question Volta actually answers. Generic market talk, politics, memes with no hook,
personal posts, giveaways, or anything where mentioning Volta would feel forced are NOT relevant.

If relevant, write one reply that:
- adds something real (an insight, a useful fact, an answer, a genuine reaction) before anything else
- is written in first person plural as the Volta team ("we're building...", "at volta we..."), so the affiliation is clear
- mentions Volta at most once and only if it fits naturally; it is fine to not mention it at all
- is short and casual like a real person: lowercase is fine, max 220 characters, at most one emoji, no hashtags
- never tells anyone to buy, ape, or invest; no price talk, no "100x", no financial advice
- makes no claims about competitors and nothing that isn't in the facts above

If not relevant, set relevant to false and leave reply empty."""

SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "reason": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["relevant", "reason", "reply"],
    "additionalProperties": False,
}


def suggest_reply(api_key, author, text, avoid=None):
    """Return {"relevant", "reason", "reply"} for a tweet, or None if the model declined."""
    prompt = f"Tweet by @{author}:\n\n{text}"
    if avoid:
        prompt += ("\n\nThe user asked for a different reply. Do not repeat these earlier drafts; "
                   "take a different angle:\n" + "\n".join(f"- {a}" for a in avoid))

    client = anthropic.Anthropic(api_key=api_key)
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    raw = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(raw)
    data["reply"] = data.get("reply", "").strip()[:280]
    return data
