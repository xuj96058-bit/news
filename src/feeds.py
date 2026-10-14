"""Bounded, read-only collection of RSS/Atom and HKMA press-release metadata.

Only text supplied in the feed is collected. Article links are never fetched,
and a feed's ``content`` field is not assumed to be the complete article.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit
from xml.parsers import expat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import requests


DEFAULT_SOURCES = Path(__file__).resolve().parents[1] / "config" / "news_sources.json"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
USER_AGENT = "news-finance-reader/1.0 (+https://github.com/xuj96058-bit/news)"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class FeedFetchError(RuntimeError):
    """A safe error message without response bodies, credentials, or URLs."""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    kind: str
    region: str
    format: str = "rss"
    enabled: bool = True
    timezone: str = "UTC"


@dataclass(frozen=True)
class Article:
    source_id: str
    source_name: str
    source_kind: str
    region: str
    title: str
    url: str
    published_at: str | None
    summary: str
    content: str
    content_scope: str
    external_id: str

    def to_dict(self) -> dict:
        return asdict(self)


def _http_url(value: object, base: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        candidate = urljoin(base, value.strip()) if base else value.strip()
        parts = urlsplit(candidate)
        if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or any(c.isspace() for c in candidate)):
            return ""
        _ = parts.port  # Reject malformed ports too.
        return candidate
    except ValueError:
        return ""


def _source_zone(name: str):
    # These current-feed zones have fixed offsets; this also works on Windows
    # installations without an IANA timezone database.
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in {"Asia/Shanghai", "Asia/Hong_Kong"}:
            return timezone(timedelta(hours=8))
        raise ValueError("Unknown source timezone") from None


def load_sources(path: str | Path = DEFAULT_SOURCES) -> list[Source]:
    """Read and validate a JSON source list (including disabled sources)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError()
        sources = [Source(**item) for item in raw]
        seen = set()
        for source in sources:
            if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source.id)
                    or source.id in seen or not _http_url(source.url)
                    or source.format not in {"rss", "atom", "hkma_json"}
                    or not isinstance(source.enabled, bool)
                    or not all(isinstance(v, str) and v.strip()
                               for v in (source.name, source.kind, source.region))):
                raise ValueError()
            _source_zone(source.timezone)
            seen.add(source.id)
        return sources
    except (OSError, TypeError, ValueError, KeyError):
        raise ValueError("Invalid news source configuration") from None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _published(value: object, source: Source) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            # FINRA Notices supplies a publication day such as "Oct 09, 2026".
            # Use the configured source zone, never the time of collection.
            try:
                dt = datetime.strptime(value, "%b %d, %Y")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_source_zone(source.timezone))
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _check_feed_root(body: bytes) -> None:
    """Check the actual XML root, before tolerant feedparser recovery."""
    class RootFound(Exception):
        pass

    roots = []
    parser = expat.ParserCreate(namespace_separator="}")

    def start(name, attrs):
        roots.append(name)
        raise RootFound()

    parser.StartElementHandler = start
    try:
        parser.Parse(body, True)
    except RootFound:
        pass
    except expat.ExpatError:
        raise FeedFetchError("Response is not an RSS or Atom feed") from None
    allowed = {"rss", "http://www.w3.org/2005/Atom}feed",
               "http://purl.org/atom/ns#}feed", "http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"}
    if not roots or roots[0] not in allowed:
        raise FeedFetchError("Response is not an RSS or Atom feed")


def parse_feed(source: Source, body: bytes) -> list[Article]:
    """Parse feed bytes without performing any network access."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise FeedFetchError("Response exceeds the 5 MiB limit")
    _check_feed_root(body)
    parsed = feedparser.parse(body)
    if not parsed.get("version") or (parsed.get("bozo") and not parsed.entries):
        raise FeedFetchError("Feed XML could not be parsed")
    articles = []
    for entry in parsed.entries:
        title = plain_text(entry.get("title", ""))
        links = [link.get("href", "") for link in entry.get("links", [])
                 if link.get("rel", "alternate") == "alternate"]
        links.append(entry.get("link", ""))
        link = next((url for value in links if (url := _http_url(value, source.url))), "")
        if not title or not link:
            continue
        summary = plain_text(entry.get("summary", ""))
        content = "\n\n".join(filter(None, (plain_text(item.get("value", ""))
                                           for item in entry.get("content", []))))
        scope = "feed_content" if content else "summary" if summary else "title_only"
        published = next((date for key in ("published", "updated", "created")
                          if (date := _published(entry.get(key), source))), None)
        articles.append(Article(
            source.id, source.name, source.kind, source.region, title, link,
            published, summary, content, scope, str(entry.get("id", "")),
        ))
    return articles


def parse_hkma(source: Source, body: bytes) -> list[Article]:
    """HKMA public/press-releases schema: header.success, result.records."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise FeedFetchError("Response exceeds the 5 MiB limit")
    try:
        payload = json.loads(body)
        if payload["header"]["success"] is not True:
            raise ValueError()
        records = payload["result"]["records"]
        if not isinstance(records, list):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise FeedFetchError("HKMA response has an invalid schema or API error") from None
    articles = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = plain_text(record.get("title", ""))
        link = _http_url(record.get("link", ""), source.url)
        if title and link:
            articles.append(Article(
                source.id, source.name, source.kind, source.region, title, link,
                _published(record.get("date"), source), "", "", "title_only", link,
            ))
    return articles


def fetch_source(source: Source, *, timeout: float = 15, session=None) -> list[Article]:
    """Fetch at most 5 MiB, retrying a transient failure once after 0.5 s.

    HTTP 4xx other than 429 are not retried. Errors are deliberately sanitized.
    The caller owns an injected session; sessions created here are closed here.
    """
    if not _http_url(source.url) or source.format not in {"rss", "atom", "hkma_json"}:
        raise FeedFetchError("Invalid feed source")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    client = session if session is not None else requests.Session()
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, text/xml;q=0.9"}
    try:
        for attempt in range(2):
            response = None
            try:
                response = client.get(source.url, headers=headers, timeout=timeout, stream=True)
                status = response.status_code
                if status in _RETRY_STATUSES and attempt == 0:
                    response.close()
                    time.sleep(0.5)
                    continue
                if status < 200 or status >= 300:
                    raise FeedFetchError(f"HTTP {status}")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise FeedFetchError("Response exceeds the 5 MiB limit")
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise FeedFetchError("Response exceeds the 5 MiB limit")
                    chunks.append(chunk)
                body = b"".join(chunks)
                return parse_hkma(source, body) if source.format == "hkma_json" else parse_feed(source, body)
            except requests.RequestException:
                if attempt == 0:
                    if response is not None:
                        response.close()
                    time.sleep(0.5)
                    continue
                raise FeedFetchError("Network request failed after 2 attempts") from None
            finally:
                if response is not None:
                    response.close()
    finally:
        if session is None:
            client.close()
    raise FeedFetchError("Feed could not be fetched")
