"""Bounded adapters for public publisher pages, without private or invented APIs.

Verified 2026-10-16: Apollo data-itemDetails, UBS PodcastSeries JSON-LD,
BlackRock componentprops article cards, JPM regional Guide PDF anchors, and
HKEX's official title-search form and load-more GET in
/ncms/js/titlesearch_research.js (rowRange is a cumulative record limit).
These collect the currently visible window; they do not promise archive coverage.
No linked PDF/audio is downloaded or represented as extracted full text.
"""

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

import requests

from .feeds import (FeedFetchError, MAX_RESPONSE_BYTES, USER_AGENT, _http_url,
                    _published, _source_zone, plain_text)


PUBLIC_FORMATS = {"apollo_html", "ubs_html", "blackrock_html", "jpm_html", "hkex_announcements"}


class CollectionBatch(list):
    """Articles plus non-content coverage diagnostics for the ingest report."""

    def __init__(self, items=(), **diagnostics):
        super().__init__(items)
        self.diagnostics = diagnostics


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def nodes(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in child.attrs.get("class", "").split()):
                    yield child
                yield from child.nodes(tag, cls)

    def text(self):
        return " ".join(" ".join(c.text() if isinstance(c, Node) else c for c in self.children).split())


class Document(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(body.decode("utf-8-sig", errors="replace"))
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def _json(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _item(source, title, url, date=None, summary="", content="", scope=None, **metadata):
    url = _http_url(url, source.url)
    title = plain_text(title)
    host = urlsplit(url).hostname or ""
    official = {"apollo_html": "apollo.com", "ubs_html": "ubs.com", "blackrock_html": "blackrock.com",
                "jpm_html": "am.jpmorgan.com", "hkex_announcements": "hkexnews.hk"}[source.format]
    if not title or not url or not (host == official or host.endswith("." + official)):
        return None
    summary, content = plain_text(summary), plain_text(content)
    return {"source_id": source.id, "source_name": source.name, "source_kind": source.kind,
            "region": source.region, "title": title, "url": url,
            "published_at": _published(date, source), "summary": summary, "content": content,
            "content_scope": scope or ("partial_text" if content else "summary" if summary else "title_only"),
            "external_id": url, "provider": source.name, "acquisition_channel": "official_public_page",
            "source_page": source.url, **metadata}


def parse_public_research(source, body):
    """Parse only individual items explicitly present in publisher HTML."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise FeedFetchError("Response exceeds the 5 MiB limit")
    root = Document(body).root
    items = []
    if source.format == "apollo_html":
        for node in root.nodes():
            record = _json(node.attrs.get("data-itemdetails"))
            if not isinstance(record, dict) or not record.get("blogId"):
                continue
            author = record.get("aboutTheAuthor") or {}
            authors = [" ".join(str(author.get(k) or "") for k in ("firstName", "lastName")).strip()]
            items.append(_item(source, record.get("title"), record.get("detailLink"), record.get("blogDate"),
                               record.get("description"), record.get("content"),
                               institution="Apollo", author=", ".join(a for a in authors if a),
                               topics=[record["eyebrowText"]] if record.get("eyebrowText") else []))
        # Text-only posts do not have image-carousel data-itemDetails.
        known = {item["url"] for item in items if item}
        for node in root.nodes(cls="blog-detail"):
            link = next(node.nodes("a", "blog-detail-info-title"), None)
            if link is None or _http_url(link.attrs.get("href"), source.url) in known:
                continue
            date_node = next(node.nodes(cls="blog-detail-info-date"), None)
            content_node = next(node.nodes(cls="blog-detail-content-fragments"), None)
            date = None
            if date_node:
                try:
                    date = datetime.strptime(date_node.text(), "%B %d, %Y").date().isoformat()
                except ValueError:
                    pass
            items.append(_item(source, link.text(), link.attrs.get("href"), date,
                               content=content_node.text() if content_node else "", institution="Apollo"))
    elif source.format == "ubs_html":
        for node in root.nodes("script"):
            if node.attrs.get("type") != "application/ld+json":
                continue
            record = _json(node.text())
            if not isinstance(record, dict) or record.get("@type") != "PodcastSeries":
                continue
            for episode in record.get("hasPart", []):
                if not isinstance(episode, dict) or episode.get("@type") != "PodcastEpisode":
                    continue
                media = episode.get("associatedMedia") or {}
                items.append(_item(source, episode.get("name"), media.get("contentUrl"),
                                   episode.get("datePublished"), episode.get("description"),
                                   institution="UBS", author="UBS Chief Investment Office",
                                   document_type="podcast_summary"))
    elif source.format == "blackrock_html":
        for node in root.nodes():
            props = _json(node.attrs.get("componentprops"))
            if not isinstance(props, dict):
                continue
            for record in props.get("articles", []):
                if not isinstance(record, dict):
                    continue
                date = record.get("publicationDate")
                try:
                    date = datetime.fromtimestamp(date / 1000, timezone.utc).isoformat() if isinstance(date, (int, float)) else date
                except (ValueError, OverflowError, OSError):
                    date = None
                items.append(_item(source, record.get("title"), record.get("link"), date, record.get("summary"),
                                   institution="BlackRock", author=", ".join(a["label"] for a in record.get("authors", [])
                                                                           if isinstance(a, dict) and a.get("label"))))
    elif source.format == "jpm_html":
        for node in root.nodes("a"):
            url = node.attrs.get("href", "")
            if "/guide-to-the-markets/" not in url or not urlsplit(url).path.lower().endswith(".pdf"):
                continue
            heading = next(node.nodes("h2"), None)
            title = heading.text() if heading else re.sub(r"\s*Read more.*", "", node.text()).strip()
            if title:
                items.append(_item(source, "Guide to the Markets — " + title, url,
                                   scope="document_link", institution="J.P. Morgan",
                                   document_type="pdf", coverage_note="Regional report link only; PDF text and edition date not extracted"))
    else:
        raise FeedFetchError("Unsupported public research format")
    # Repeated image-carousel metadata must not multiply the same article.
    unique = {item["url"]: item for item in items if item}
    if not unique:
        raise FeedFetchError("Publisher page contained no recognized items; layout or access may have changed")
    return CollectionBatch(unique.values(), coverage="Currently visible publisher items only; no historical backfill")


def parse_hkex_announcements(source, body):
    root = Document(body).root
    items = []
    for row in root.nodes("tr"):
        link = next((a for a in row.nodes("a") if "/listedco/listconews/" in a.attrs.get("href", "")), None)
        if link is None:
            continue
        def cell(cls):
            node = next(row.nodes(cls=cls), None)
            if node is None:
                return ""
            # Mobile labels are separate spans; remove only their known prefix.
            text = node.text()
            return re.sub(r"^(Release Time|Stock Code|Stock Short Name):\s*", "", text)
        code, company = cell("stock-short-code"), cell("stock-short-name")
        date = cell("release-time")
        try:
            date = datetime.strptime(date, "%d/%m/%Y %H:%M").replace(tzinfo=_source_zone(source.timezone)).isoformat()
        except ValueError:
            date = None
        item = _item(source, f"{code} {company} — {link.text()}", link.attrs["href"], date,
                     scope="announcement_metadata", tickers=[f"{code}.HK"] if re.fullmatch(r"\d{4,5}", code) else [],
                     publisher=company, document_type="issuer_announcement", headline_category=cell("headline"))
        if item:
            items.append(item)
    text = root.text()
    match = re.search(r"Total records found:\s*([\d,]+)", text)
    if not match:
        raise FeedFetchError("HKEX search result schema changed or access was restricted")
    total = int(match.group(1).replace(",", ""))
    if total and not items:
        raise FeedFetchError("HKEX returned records without recognizable announcement links")
    missing = max(0, total - len(items))
    return CollectionBatch(items, available=total, upstream_limited=missing, gap=bool(missing),
                           coverage="HKEX recent seven-day search, first result page only; documents not downloaded")


def parse_hkex_json(source, body):
    """Schema used by the official HKEX search page's load-more GET request."""
    try:
        payload = json.loads(body)
        records = payload["result"]
        records = json.loads(records) if isinstance(records, str) else records
        total, loaded = int(payload["recordCnt"]), int(payload["loadedRecord"])
        if not isinstance(records, list) or total < 0 or loaded < 0 or loaded != len(records):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise FeedFetchError("HKEX response has an invalid announcement schema") from None
    items = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if not plain_text(record.get("TITLE")) or "/listedco/listconews/" not in str(record.get("FILE_LINK") or ""):
            continue
        code, company = plain_text(record.get("STOCK_CODE")), plain_text(record.get("STOCK_NAME"))
        try:
            date = datetime.strptime(record.get("DATE_TIME", ""), "%d/%m/%Y %H:%M").replace(tzinfo=_source_zone(source.timezone)).isoformat()
        except (ValueError, TypeError):
            date = None
        item = _item(source, f"{code} {company} — {plain_text(record.get('TITLE'))}", record.get("FILE_LINK"), date,
                     scope="announcement_metadata", tickers=[f"{code}.HK"] if re.fullmatch(r"\d{4,5}", code) else [],
                     publisher=company, document_type="issuer_announcement", headline_category=plain_text(record.get("LONG_TEXT")))
        if item:
            items.append(item)
    if records and not items:
        raise FeedFetchError("HKEX returned records without recognizable announcement links")
    missing = max(0, total - len(items))
    return CollectionBatch(items, available=total, upstream_limited=missing, gap=bool(missing or payload.get("hasNextRow")),
                           coverage="HKEX recent seven-day search, at most 500 latest results; documents not downloaded")


def fetch_public_research(source, *, timeout=15, session=None, now=None):
    """One bounded page request; retries and per-source continuation are scheduled."""
    if source.format not in PUBLIC_FORMATS or timeout <= 0:
        raise ValueError("Invalid public research source or timeout")
    client = session or requests.Session()
    response = None
    try:
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
        if source.format == "hkex_announcements":
            today = (now or datetime.now(timezone.utc)).astimezone(_source_zone(source.timezone)).date()
            payload = {"lang": "en", "market": "SEHK", "searchType": "0", "documentType": "-1",
                       "t1code": "-2", "t2Gcode": "-2", "t2code": "-2", "stockId": "-1", "category": "0", "title": "",
                       "fromDate": (today - timedelta(days=6)).strftime("%Y%m%d"), "toDate": today.strftime("%Y%m%d"),
                       "rowRange": "500", "sortDir": "0", "sortByOptions": "DateTime"}
            response = client.get(source.url, params=payload, headers=headers, timeout=timeout, stream=True)
        else:
            response = client.get(source.url, headers=headers, timeout=timeout, stream=True)
        if not 200 <= response.status_code < 300:
            raise FeedFetchError(f"HTTP {response.status_code}")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise FeedFetchError("Response exceeds the 5 MiB limit")
            chunks.append(chunk)
        body = b"".join(chunks)
        batch = parse_hkex_json(source, body) if source.format == "hkex_announcements" else parse_public_research(source, body)
        if source.format == "hkex_announcements":
            batch.diagnostics.update(window_from=payload["fromDate"], window_to=payload["toDate"])
        return batch
    except requests.RequestException:
        raise FeedFetchError("Public source network request failed") from None
    finally:
        if response is not None:
            response.close()
        if session is None:
            client.close()
