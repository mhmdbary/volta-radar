# Install Bary Radar for a new project

Each project gets its own copy of this repo, its own Telegram bot and its own keys. Nothing is shared between
projects, so one client's data never appears in another's reports.

## 1. Make the copy

Copy this repo to a new one named `bary-radar-<project>`. Do not copy `state.json`, `stats.json`, `mywork.json`
or `config.json`.

## 2. Make the Telegram bot

In @BotFather: `/newbot`, then `/setuserpic` with `bary_radar_bot.png`. Keep the token.
Message the bot once, then run `python notifier.py --chat-id` to get your chat id.

## 3. Fill in `settings.json`

The `project` block is the only part that describes the project:

| Field | Meaning |
|---|---|
| `name`, `ticker`, `handle`, `site` | The project's name, token, X account and website |
| `category` | What it is: launchpad, perp dex, wallet... |
| `topics` | Subjects where mentioning it is natural; a reply outside them stays light |
| `eco_words` | Tags containing these get the green ecosystem marker and the fast lane |
| `team` | Accounts to leave out of reports |
| `partners` | Existing partners, left out of "who to talk to" |
| `not_leads` | Competitors and big names, also left out |
| `facts` | What the AI may say. Only verified facts, from the project's own posts |
| `angles` | One selling point per line; drafts rotate through them |

Also set `mention_terms`, `launch_date`, `launch_name`, `watch_follows_of`, `my_handle` and `work_start`.

## 4. Fill in the accounts

`handles.txt` lists the accounts to track, one per line. `tags.json` maps each lowercase handle to its label.

## 5. Add the keys

As GitHub Secrets on the new repo, never in a file or in chat:
`TWITTERAPI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_ID`, `ANTHROPIC_API_KEY`, and
optionally `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` for one-tap like and repost.

## 6. Start it

Run the `Bary Radar` workflow once from the Actions tab. It restarts itself after that.
Press `📡 تقرير المشروع` in the bot to confirm it works.
