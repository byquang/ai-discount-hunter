from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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


@dataclass(frozen=True)
class Candidate:
    source_id: str
    title: str
    text: str
    url: str
    published: datetime | None = None
    links: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, str]:
        return self.source_id, url_key(self.url)
