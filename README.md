# AI Promo Hunter

Finds time-limited free credits, free paid-plan months, and 100%-off offers for AI and developer tools. The Python bot is scheduled every 30 minutes on GitHub Actions; Gemini filters out noise and Telegram delivers updates in English.

## Set up on GitHub

1. Create a [Gemini API key](https://aistudio.google.com/apikey) in a **free-tier project without billing**. The default model is `gemini-3.5-flash-lite`. To change it, set the Actions **variable** `GEMINI_MODEL` to another compatible free model.
2. Create a Telegram bot with [BotFather](https://t.me/BotFather). Send the new bot `/start`, or add it to your destination group and send it a command. Use [getUpdates](https://core.telegram.org/bots/api#getupdates) to read the destination's `message.chat.id`; don't share the bot token or paste it into a chat. For a channel, add the bot as an admin with permission to post and use the channel's `@username` or numeric ID.
3. Add these [repository Actions secrets](https://github.com/byquang/ai-discount-hunter/settings/secrets/actions):

   | Secret | Required | Value |
   | --- | --- | --- |
   | `GEMINI_API_KEY` | Yes | Gemini Developer API key from step 1 |
   | `TELEGRAM_BOT_TOKEN` | Yes | BotFather token from step 2 |
   | `TELEGRAM_CHAT_ID` | Yes | Telegram destination from step 2 |
   | `TAVILY_API_KEY` | No | Tavily's free Researcher plan key |
   | `BRAVE_API_KEY` | No | Brave Search key; used only if Tavily is unset |

4. Once the workflow is on the default branch, open [AI Promo Hunter](https://github.com/byquang/ai-discount-hunter/actions/workflows/hunt.yml) → **Run workflow**. Select **Send one test message instead of hunting** to verify Telegram independently; clear it to run a real hunt.

The hunt uses its job-scoped `GITHUB_TOKEN` to push only changes to `seen.json`. No personal access token is needed. The default branch and Actions policy must allow the workflow's state push; restrictions that block all direct pushes will also block this step. Pull requests run credential-free tests, not the hunt.

The `*/30` cron uses Kathmandu's fixed UTC+05:45 offset, targeting `:15` and `:45` UTC (`:15` and `:45` in Vietnam too). This avoids GitHub's [top-of-hour scheduling peak](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows-running-at-unexpected-times). GitHub may still delay or drop runs.

## Sources and cost

- **Hacker News:** The public Algolia API searches recent stories for promotion terms, without a key.
- **Official feeds:** OpenAI, Cursor, GitHub Copilot, Google AI, and Replit release/news feeds are checked without a key. These are starting sources, not a vendor allowlist.
- **Open web:** With Tavily or Brave, the bot also searches vendor sites and other indexed pages across the web for AI and developer-tool promotions. It looks back up to 14 days, so a deal announced earlier in the week can still be found while active.
- **Reddit:** [Reddit retired public RSS on November 13, 2026](https://techcrunch.com/2026/09/30/reddit-is-killing-rss-feeds-and-ending-public-api-access-because-of-ai-bots/). With an optional search key, the bot looks for indexed posts in `r/ClaudeAI`, `r/OpenAI`, `r/cursor`, `r/LocalLLaMA`, `r/vibecoding`, and `r/ArtificialInteligence`. This is not a live feed and can miss new posts.
- **X and Threads:** The same optional key searches for indexed deals across X and Threads. Scheduled runs rotate open-web, X/Threads, and Reddit searches; one request at most per search run, typically about 744 total requests or fewer in a 31-day month. Manual hunts query all three categories and count against your quota. Without a key, the bot still scans Hacker News and official feeds.

Use [Gemini Free Tier](https://ai.google.dev/gemini-api/docs/pricing) and leave optional web search off or use [Tavily's free allocation](https://docs.tavily.com/documentation/api-credits). [Brave includes monthly credits](https://brave.com/search/api/) but requires a card and may bill above those credits; leave it unset if you need to rule out overages. GitHub-hosted standard runners are free on public repos; **this private repo uses your account's included compute**. Check your [GitHub Actions budget](https://docs.github.com/en/billing/how-tos/set-up-budgets) and prevent billable usage before turning on the schedule. The bot stops getting runs if GitHub blocks usage after its allowance; schedule timing is best-effort.

## Behavior

Each completed hunt sends new offer alerts or one “No new promotions found” message. If some sources fail, the empty-scan message notes the reduced coverage. A hunt that fails before it can determine a result does not send an empty-scan message; see its GitHub Actions log.

The bot looks back two days on community sources and up to 14 days on official feeds and open-web search. It sends up to 12 unseen candidates in one structured Gemini request per run. It rejects expired offers, evergreen free tiers, paid discounts, and tutorials. For claim windows relative to a post (such as “within 24 hours”), it calculates the deadline from publication time and skips the offer if that time is unknown. X publication times come from post IDs. Alerts distinguish a claim link found in the source data from a link to the source post itself. Search and feed coverage is not exhaustive; verify an offer on the vendor's site before entering credentials or payment details.

`seen.json` retains the latest 500 classified entries, matched by source ID and normalized URL, plus the confirmed claim URL when available. A failed Telegram send is left eligible for retry; GitHub serializes hunts to prevent state races. Deduplication can't identify unrelated links for the same deal or guarantee delivery exactly once if a send succeeds but the state push fails.

## Run locally

Python 3.10+:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v

# With the required environment variables set:
.venv/bin/python -m promo_hunter
# To verify only the Telegram credentials:
.venv/bin/python -m promo_hunter --test-telegram
```

Tests mock external services and need no keys. Live runs log source and provider failures without printing credentials.