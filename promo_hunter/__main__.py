import argparse
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.genai.errors import APIError

from .candidate import Candidate, url_key
from .filtering import DEFAULT_MODEL, MAX_BATCH, Offer, classify, keyword_score
from .sources import discover, scheduled_web_scope
from .state import Seen, reserve_search_hour
from .telegram import TelegramError, format_alert, format_empty_scan, send_message

LOG = logging.getLogger(__name__)


def select_candidates(candidates: list[Candidate], seen: Seen) -> list[Candidate]:
    ranked = sorted(
        ((keyword_score(post.title, post.text), post) for post in candidates),
        key=lambda item: (
            item[0],
            item[1].source_id.startswith("official:"),
            item[1].published.timestamp() if item[1].published else 0,
        ),
        reverse=True,
    )
    batch = []
    selected_keys = set()

    def take(limit: int, *, open_web_only: bool = False) -> None:
        for score, post in ranked:
            if not score or len(batch) == limit:
                break
            if open_web_only and not post.source_id.startswith("open-web:"):
                continue
            if seen.contains(post.keys) or selected_keys.intersection(post.keys):
                continue
            batch.append(post)
            selected_keys.update(post.keys)

    take(MAX_BATCH // 4, open_web_only=True)
    take(MAX_BATCH)
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
    logging.getLogger("google_genai").setLevel(logging.WARNING)

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
                notify("*AI Promo Hunter:* Telegram connection test passed")
                return 0
            seen = Seen(Path("seen.json"))
            now = datetime.now(timezone.utc)
            search_keys = (env["TAVILY_API_KEY"], env["BRAVE_API_KEY"])
            if os.getenv("GITHUB_EVENT_NAME") == "schedule":
                web_targets = ()
                if any(search_keys) and reserve_search_hour(Path("web-search-hour.txt"), now):
                    web_targets = (scheduled_web_scope(now),)
                result = discover(session, now, *search_keys, web_targets, include_feeds=now.hour % 2 == 0)
            else:
                result = discover(session, now, *search_keys)
            candidates = result.candidates
            batch = select_candidates(candidates, seen)
            sent = 0
            if batch:
                try:
                    offers = classify(env["GEMINI_API_KEY"], env["GEMINI_MODEL"] or DEFAULT_MODEL, batch, now)
                    sent = process_batch(batch, offers, seen, notify)
                finally:
                    seen.save()
            if not sent:
                notify(format_empty_scan(result.partial))
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
