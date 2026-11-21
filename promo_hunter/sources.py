import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

from .candidate import Candidate, web_url

SUBREDDITS = ("ClaudeAI", "OpenAI", "cursor", "LocalLLaMA", "vibecoding", "ArtificialInteligence")
HN_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_QUERIES = ("free credits", "free month", "100% off", "promo code", "giveaway")
PROMO_TERMS = '("free credits" OR "free month" OR "100% off")'
WEB_TERMS = PROMO_TERMS + ' ("AI" OR "Devin" OR "Claude" OR "Cursor")'
WEB_DOMAINS = ["x.com", "threads.net", "threads.com"]
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


def web_posts(results: list[dict], reddit: bool = False) -> list[Candidate]:
    posts = []
    allowed_subreddits = {name.lower() for name in SUBREDDITS}
    for result in results:
        url = web_url(result.get("url"))
        title = result.get("title")
        if not url or not title:
            continue
        parts = urlsplit(url)
        host = parts.hostname or ""
        source_id = f"web:{url}"
        if reddit:
            path = parts.path.lower().strip("/").split("/")
            if (
                (host != "reddit.com" and not host.endswith(".reddit.com"))
                or len(path) < 4
                or path[0] != "r"
                or path[1] not in allowed_subreddits
                or path[2] != "comments"
                or not path[3]
            ):
                continue
            source_id = f"reddit:{path[3]}"
        elif not any(host == domain or host.endswith("." + domain) for domain in WEB_DOMAINS):
            continue
        body, _ = post_content(result.get("content") or result.get("description") or "")
        published = timestamp(result.get("published_date") or result.get("page_age"))
        posts.append(Candidate(source_id, title, body, url, published))
    return posts


def fetch_web(
    session: requests.Session, tavily_key: str, brave_key: str, since: datetime, reddit: bool
) -> list[Candidate]:
    domains = ["reddit.com"] if reddit else WEB_DOMAINS
    sites = [f"reddit.com/r/{name}/comments" for name in SUBREDDITS] if reddit else domains
    site_query = " OR ".join(f"site:{site}" for site in sites)
    query = f"({site_query}) {PROMO_TERMS if reddit else WEB_TERMS}"
    if tavily_key:
        response = session.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {tavily_key}"},
            json={
                "query": query,
                "include_domains": domains,
                "search_depth": "basic",
                "auto_parameters": False,
                "max_results": 10,
                "start_date": since.date().isoformat(),
            },
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        response.raise_for_status()
        return web_posts(response.json()["results"], reddit)
    response = session.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": brave_key},
        params={
            "q": query,
            "freshness": f"{since.date().isoformat()}to{datetime.now(timezone.utc).date().isoformat()}",
            "count": 10,
        },
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    response.raise_for_status()
    return web_posts(response.json().get("web", {}).get("results", []), reddit)


def discover(
    session: requests.Session,
    since: datetime,
    tavily_key: str = "",
    brave_key: str = "",
    web_targets: tuple[bool, ...] = (False, True),
) -> list[Candidate]:
    def fetch_hn(query: str) -> list[Candidate]:
        response = session.get(
            HN_URL,
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{int(since.timestamp())}",
                "hitsPerPage": 30,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return hn_posts(response.json())

    candidates = []
    succeeded = 0

    def ingest(label, fetch, *args) -> bool:
        nonlocal succeeded
        try:
            candidates.extend(fetch(*args))
            succeeded += 1
        except requests.HTTPError as exc:
            status = exc.response.status_code
            LOG.warning("%s returned HTTP %s", label, status)
            return status != 429
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            LOG.warning("%s unavailable (%s)", label, type(exc).__name__)
        return True

    for query in HN_QUERIES:
        if not ingest("Hacker News", fetch_hn, query):
            break
    if tavily_key or brave_key:
        provider = "Tavily" if tavily_key else "Brave Search"
        for reddit in web_targets:
            label = f"{provider} ({'Reddit' if reddit else 'X/Threads'})"
            if not ingest(label, fetch_web, session, tavily_key, brave_key, since, reddit):
                break
    if not succeeded:
        raise RuntimeError("All promotion sources failed")
    return [post for post in candidates if post.published is None or post.published >= since]
