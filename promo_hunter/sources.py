import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from .candidate import Candidate, web_url

SUBREDDITS = ("ClaudeAI", "OpenAI", "cursor", "LocalLLaMA", "vibecoding", "ArtificialInteligence")
HN_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_QUERIES = ("free credits", "free month", "100% off", "promo code", "giveaway")
PROMO_TERMS = '("free credits" OR "free month" OR "100% off")'
WEB_TERMS = PROMO_TERMS + ' ("AI" OR "Devin" OR "Claude" OR "Cursor")'
WEB_DOMAINS = ["x.com", "threads.net", "threads.com"]
OPEN_WEB_TERMS = (
    '("free credits" OR "free month" OR "100% off" OR "free until" OR "free for" OR giveaway) '
    '("AI" OR "developer tool" OR "coding agent" OR "AI assistant")'
)
OFFICIAL_FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Cursor": "https://cursor.com/changelog/rss.xml",
    "GitHub Copilot": "https://github.blog/ai-and-ml/github-copilot/feed/",
    "Google AI": "https://blog.google/innovation-and-ai/technology/ai/rss/",
    "Replit": "https://replit.com/blog/feed.xml",
}
COMMUNITY_WINDOW = timedelta(days=2)
PROMOTION_WINDOW = timedelta(days=14)
MAX_FEED_BYTES = 2 * 1024 * 1024
SearchScope = Literal["web", "social", "reddit"]
X_EPOCH_MS = 1288834974657
X_STATUS_PATH = re.compile(r"^/(?:i/web|[^/]+)/status/([0-9]{1,19})(?:/|$)")
TIMEOUT = (5, 15)
LOG = logging.getLogger(__name__)


class PostHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (link := web_url(dict(attrs).get("href"))):
            self.links.append(link)


def post_content(raw: str) -> tuple[str, tuple[str, ...]]:
    post = PostHTML()
    post.feed(raw[:16000])
    post.close()
    return " ".join(" ".join(post.text).split())[:1800], tuple(dict.fromkeys(post.links))[:8]


def timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            pass
    return None


def hn_posts(data: dict) -> list[Candidate]:
    posts = []
    for hit in data["hits"]:
        if not hit.get("objectID") or not hit.get("title"):
            continue
        post_id = str(hit["objectID"])
        discussion = f"https://news.ycombinator.com/item?id={post_id}"
        url = web_url(hit.get("url")) or discussion
        body, links = post_content(hit.get("story_text") or "")
        posts.append(Candidate(
            f"hn:{post_id}", hit["title"], body, url, timestamp(hit.get("created_at")), links
        ))
    return posts


class RSSBuilder(ElementTree.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("Unexpected document type in RSS")


def official_posts(raw: bytes, since: datetime, now: datetime) -> list[Candidate]:
    try:
        parser = ElementTree.XMLParser(target=RSSBuilder())
        channel = ElementTree.fromstring(raw, parser=parser).find("channel")
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid RSS") from exc
    if channel is None:
        raise ValueError("Missing RSS channel")
    posts = []
    for item in channel.iterfind("item"):
        url, title = web_url(item.findtext("link")), item.findtext("title")
        if not url or not title:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
        except (TypeError, ValueError, OverflowError):
            continue
        published = published.replace(tzinfo=published.tzinfo or timezone.utc)
        if not since <= published <= now:
            continue
        body, links = post_content(
            item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            or item.findtext("description", "")
        )
        posts.append(Candidate(f"official:{url}", title, body, url, published.astimezone(timezone.utc), links))
    return posts


def fetch_official(session: requests.Session, url: str, since: datetime, now: datetime) -> list[Candidate]:
    with session.get(url, stream=True, timeout=TIMEOUT, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected RSS response")
        payload = bytearray()
        for chunk in response.iter_content(65536):
            payload.extend(chunk)
            if len(payload) > MAX_FEED_BYTES:
                raise ValueError("RSS feed too large")
        return official_posts(bytes(payload), since, now)


def web_posts(results: list[dict], scope: SearchScope = "social") -> list[Candidate]:
    posts = []
    allowed_subreddits = {name.lower() for name in SUBREDDITS}
    for result in results:
        url = web_url(result.get("url"))
        title = result.get("title")
        if not url or not title:
            continue
        published = timestamp(result.get("published_date") or result.get("page_age"))
        parts = urlsplit(url)
        host = parts.hostname or ""
        source_id = f"web:{url}"
        social_host = any(host == domain or host.endswith("." + domain) for domain in WEB_DOMAINS)
        reddit_host = host == "reddit.com" or host.endswith(".reddit.com")
        if scope == "reddit":
            path = parts.path.lower().strip("/").split("/")
            if (
                not reddit_host
                or len(path) < 4
                or path[0] != "r"
                or path[1] not in allowed_subreddits
                or path[2] != "comments"
                or not path[3]
            ):
                continue
            source_id = f"reddit:{path[3]}"
        elif (scope == "social" and not social_host) or (scope == "web" and (social_host or reddit_host)):
            continue
        elif host == "x.com" or host.endswith(".x.com"):
            status = X_STATUS_PATH.match(parts.path)
            if not status or not 0 < (post_id := int(status[1])) < 2**63:
                continue
            # X's post ID encodes publication time, unlike the search index date.
            published = datetime.fromtimestamp(((post_id >> 22) + X_EPOCH_MS) / 1000, timezone.utc)
        body, links = post_content(result.get("content") or result.get("description") or "")
        posts.append(Candidate(source_id, title, body, url, published, links))
    return posts


def fetch_web(
    session: requests.Session, tavily_key: str, brave_key: str, since: datetime, scope: SearchScope
) -> list[Candidate]:
    domains = ["reddit.com"] if scope == "reddit" else WEB_DOMAINS
    sites = [f"reddit.com/r/{name}/comments" for name in SUBREDDITS] if scope == "reddit" else domains
    site_query = " OR ".join(f"site:{site}" for site in sites)
    terms = PROMO_TERMS if scope == "reddit" else WEB_TERMS
    query = OPEN_WEB_TERMS if scope == "web" else f"({site_query}) {terms}"
    if tavily_key:
        response = session.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {tavily_key}"},
            json={
                "query": query,
                **({"exclude_domains": [*WEB_DOMAINS, "reddit.com"]} if scope == "web"
                   else {"include_domains": domains}),
                "search_depth": "basic",
                "auto_parameters": False,
                "max_results": 10,
                "start_date": since.date().isoformat(),
            },
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        response.raise_for_status()
        return web_posts(response.json()["results"], scope)
    response = session.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": brave_key},
        params={
            "q": query if scope != "web" else f"{query} " + " ".join(
                f"-site:{domain}" for domain in [*WEB_DOMAINS, "reddit.com"]
            ),
            "freshness": f"{since.date().isoformat()}to{datetime.now(timezone.utc).date().isoformat()}",
            "count": 10,
        },
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    response.raise_for_status()
    return web_posts(response.json().get("web", {}).get("results", []), scope)


def scheduled_web_scopes(now: datetime) -> tuple[SearchScope, ...]:
    return {0: ("web",), 2: ("social",), 4: ("web",), 6: ("reddit",)}.get(now.hour % 8, ())


def discover(
    session: requests.Session,
    now: datetime,
    tavily_key: str = "",
    brave_key: str = "",
    web_targets: tuple[SearchScope, ...] = ("web", "social", "reddit"),
    include_feeds: bool = True,
) -> list[Candidate]:
    community_since = now - COMMUNITY_WINDOW
    promo_since = now - PROMOTION_WINDOW

    def fetch_hn(query: str) -> list[Candidate]:
        response = session.get(
            HN_URL,
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{int(community_since.timestamp())}",
                "hitsPerPage": 30,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return hn_posts(response.json())

    candidates = []
    succeeded = 0

    def ingest(label, cutoff, fetch, *args) -> bool:
        nonlocal succeeded
        try:
            fresh = [
                post for post in fetch(*args) if post.published is None or cutoff <= post.published <= now
            ]
            candidates.extend(fresh)
            succeeded += 1
            LOG.debug("%s returned %s in-window results", label, len(fresh))
        except requests.HTTPError as exc:
            status = exc.response.status_code
            LOG.warning("%s returned HTTP %s", label, status)
            return status != 429
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            LOG.warning("%s unavailable (%s)", label, type(exc).__name__)
        return True

    if include_feeds:
        for vendor, url in OFFICIAL_FEEDS.items():
            ingest(f"{vendor} RSS", promo_since, fetch_official, session, url, promo_since, now)
    for query in HN_QUERIES:
        if not ingest("Hacker News", community_since, fetch_hn, query):
            break
    if tavily_key or brave_key:
        provider = "Tavily" if tavily_key else "Brave Search"
        for scope in web_targets:
            cutoff = promo_since if scope == "web" else community_since
            if not ingest(f"{provider} ({scope})", cutoff, fetch_web, session, tavily_key, brave_key, cutoff, scope):
                break
    if not succeeded:
        raise RuntimeError("All promotion sources failed")
    return candidates
