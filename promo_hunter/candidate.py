import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

X_EPOCH_MS = 1288834974657
X_STATUS_PATH = re.compile(r"^/(?:i/web|[^/]+)/status/([0-9]{1,19})(?:/|$)")
REDDIT_POST_PATH = re.compile(r"/(?:r/[^/]+/)?comments/([a-z0-9]+)(?:/[^/]+)?/?", re.IGNORECASE)


def web_url(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return None
    if any(c.isspace() or ord(c) < 32 or c in '<>\\"' for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.port not in {None, 80, 443}
        ):
            return None
    except ValueError:
        return None
    return value


def url_key(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.lower().startswith("utm_")
        and name.lower() not in {"fbclid", "gclid", "msclkid"}
    ]
    host = parts.netloc.lower().removeprefix("www.")
    host = host.removesuffix(":443") if parts.scheme == "https" else host.removesuffix(":80")
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def x_post_time(url: str) -> datetime | None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host == "x.com" or host.endswith(".x.com"):
        status = X_STATUS_PATH.match(parts.path)
        if status and 0 < (post_id := int(status[1])) < 2**63:
            return datetime.fromtimestamp(((post_id >> 22) + X_EPOCH_MS) / 1000, timezone.utc)
    return None


def reddit_post_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host == "reddit.com" or host.endswith(".reddit.com"):
        if post := REDDIT_POST_PATH.fullmatch(parts.path):
            return post[1].lower()
    return None


@dataclass(frozen=True)
class Candidate:
    source_id: str
    title: str
    text: str
    url: str
    published: datetime | None = None
    links: tuple[str, ...] = ()
    updated: datetime | None = None

    @property
    def freshness(self) -> datetime | None:
        return self.published or self.updated

    @property
    def keys(self) -> tuple[str, ...]:
        keys = (self.source_id, url_key(self.url))
        if post_id := reddit_post_id(self.url):
            reddit_key = f"reddit:{post_id}"
            if reddit_key != self.source_id:
                return (*keys, reddit_key)
        return keys

    @property
    def source_published(self) -> datetime | None:
        return x_post_time(self.url) or self.published
