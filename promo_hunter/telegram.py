import re

import requests

from .filtering import Offer

MARKDOWN = re.compile(r"([_\*\[\]\(\)~`>#+\-=|{}.!\\])")


class TelegramError(RuntimeError):
    pass


def escape(text: str, limit: int) -> str:
    return MARKDOWN.sub(lambda match: "\\" + match[0], " ".join(text.split())[:limit])


def format_alert(offer: Offer) -> str:
    url = offer.url.replace("\\", "\\\\").replace(")", "\\)")
    link_label = "View source post" if offer.source_only else "Open claim page"
    return "\n".join([
        f"*Promotion: {escape(offer.tool_name, 75)}*",
        f"*Offer:* {escape(offer.offer_summary, 500)}",
        f"*Deadline:* {escape(offer.deadline or 'Not announced', 30)}",
        f"*How to claim:* {escape(offer.how_to_claim, 300)}",
        f"[{link_label}]({url})",
    ])


def format_empty_scan(partial: bool) -> str:
    if partial:
        return "*AI Promo Hunter:* No new promotions found in the sources checked\nSome sources could not be checked"
    return "*AI Promo Hunter:* No new promotions found in this scan"


def send_message(session: requests.Session, token: str, chat_id: str, text: str) -> None:
    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=(5, 15),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise TelegramError(f"Telegram sendMessage returned HTTP {response.status_code}")
        if response.json().get("ok") is not True:
            raise TelegramError("Telegram sendMessage was not acknowledged")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise TelegramError(f"Telegram sendMessage failed ({type(exc).__name__})") from None
