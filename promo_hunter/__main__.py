import argparse
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.genai.errors import APIError

from .candidate import Candidate, url_key
from .filtering import DEFAULT_MODEL, MAX_BATCH, Offer, classify, keyword_score
from .sources import discover
from .state import Seen
from .telegram import TelegramError, format_alert, send_message

LOG = logging.getLogger(__name__)


def select_candidates(candidates: list[Candidate], seen: Seen) -> list[Candidate]:
    ranked = sorted(
        ((keyword_score(post.title, post.text), post) for post in candidates),
        key=lambda item: (item[0], item[1].published.timestamp() if item[1].published else 0),
        reverse=True,
    )
    batch = []
    selected_keys = set()
    for score, post in ranked:
        if not score or len(batch) == MAX_BATCH:
            break
        if seen.contains(post.keys) or selected_keys.intersection(post.keys):
            continue
        batch.append(post)
        selected_keys.update(post.keys)
    return batch


def process_batch(
    batch: list[Candidate], offers: list[Offer | None], seen: Seen, notify: Callable[[str], None]
) -> int:
    sent = 0
    for post, offer in zip(batch, offers, strict=True):
        if seen.contains(post.keys):
            continue
        if offer is None:
            seen.remember(post.keys)
            continue
        claim_key = url_key(offer.url)
        if not seen.contains((claim_key,), alerted_only=True):
            notify(format_alert(offer))
            sent += 1
        seen.remember((*post.keys, claim_key), alerted=True)
        seen.save()
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Find promotions and send Telegram alerts")
    parser.add_argument("--test-telegram", action="store_true", help="Send one smoke-test message and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    env = {name: os.getenv(name, "").strip() for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY", "GEMINI_MODEL",
        "TAVILY_API_KEY", "BRAVE_API_KEY",
    )}
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    if not args.test_telegram:
        required.append("GEMINI_API_KEY")
    missing = [key for key in required if not env[key]]
    if missing:
        LOG.error("Missing required environment variables: %s", ", ".join(missing))
        return 1

    with requests.Session() as session:
        def notify(text: str) -> None:
            send_message(session, env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], text)

        try:
            if args.test_telegram:
                notify("*AI Promo Hunter:* Kiểm tra kết nối Telegram thành công")
                return 0
            seen = Seen(Path("seen.json"))
            now = datetime.now(timezone.utc)
            web_targets = (False, True)
            if os.getenv("GITHUB_EVENT_NAME") == "schedule":
                # At most half the scheduled runs use web search, alternating the source.
                web_targets = {0: (False,), 2: (True,)}.get(now.hour % 4, ())
            candidates = discover(
                session, now - timedelta(days=2), env["TAVILY_API_KEY"], env["BRAVE_API_KEY"], web_targets
            )
            batch = select_candidates(candidates, seen)
            if not batch:
                LOG.info("No new keyword matches among %s source results", len(candidates))
                return 0
            try:
                offers = classify(env["GEMINI_API_KEY"], env["GEMINI_MODEL"] or DEFAULT_MODEL, batch, now.date())
                sent = process_batch(batch, offers, seen, notify)
            finally:
                seen.save()
            LOG.info("Classified %s of %s source results; sent %s alerts", len(batch), len(candidates), sent)
            return 0
        except APIError as exc:
            LOG.error("Gemini returned HTTP %s; check key, model availability and free-tier quota", exc.code)
        except TelegramError as exc:
            LOG.error("%s", exc)
        except Exception as exc:
            # Provider exceptions can contain request URLs or bodies; log only the failure type.
            LOG.error("Hunt failed (%s)", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
