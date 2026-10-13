"""S&P 500 + Nasdaq-100 membership and bounded SEC current-filings Atom.

Membership uses explicitly attributed public Wikipedia tables, not ETF proxies
or licensed index-provider data. SEC's official ticker map supplies every CIK.
RSS entries are filing notices, not the financial statements themselves.
"""

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit

import feedparser
import requests

from .feeds import Article, FeedFetchError, Source, USER_AGENT, parse_feed


DEFAULT_UNIVERSE = Path(__file__).resolve().parents[1] / "config" / "sec_universe.json"
MEMBERSHIP_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "nasdaq100": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
}
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F")
MAX_BYTES = 5 * 1024 * 1024
PAGE_SIZE = 100


class SecConfigError(ValueError):
    """A missing contact, incomplete membership mapping, or invalid snapshot."""


def sec_user_agent(value=None):
    """Require a user-provided contact; never guess or persist their email."""
    value = value if value is not None else os.environ.get("SEC_USER_AGENT", "")
    if (not isinstance(value, str) or len(value) > 300
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            or not re.search(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", value)
            or re.search(r"@(example\.(com|org|net)|your[-a-z]*\.[a-z]+)\b", value, re.I)):
        raise SecConfigError("Set SEC_USER_AGENT to your application name and genuine contact email")
    return value


def normalize_symbol(symbol):
    """SEC represents dot share classes with a hyphen (BRK.B -> BRK-B)."""
    if not isinstance(symbol, str):
        raise SecConfigError("Invalid constituent symbol")
    symbol = symbol.strip().upper().replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,11}", symbol):
        raise SecConfigError("Invalid constituent symbol")
    return symbol


class _Tables(HTMLParser):
    """Read text cells only; unrelated/nested tables cannot masquerade as rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.depth = 0
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = []
        if self.depth != 1:
            return
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag in {"sup", "style", "script"}:
            self.hidden += 1

    def handle_data(self, text):
        if self.depth == 1 and self.cell is not None and not self.hidden:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if self.depth == 1:
            if tag in {"td", "th"} and self.cell is not None:
                if self.row is not None:
                    self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.table.append(self.row)
                self.row = None
            elif tag in {"sup", "style", "script"}:
                self.hidden = max(0, self.hidden - 1)
            elif tag == "table":
                self.tables.append(self.table)
                self.table, self.row, self.cell = None, None, None
        if tag == "table":
            self.depth = max(0, self.depth - 1)


def parse_constituents(body, index):
    """Fail on missing, ambiguous, duplicate, or implausibly sized tables."""
    if index not in MEMBERSHIP_URLS:
        raise SecConfigError("Unknown index")
    if len(body) > MAX_BYTES:
        raise SecConfigError("Membership response exceeds the 5 MiB limit")
    parser = _Tables()
    parser.feed(body.decode("utf-8"))
    symbol_label, name_label = ("Symbol", "Security") if index == "sp500" else ("Ticker", "Company")
    tables = [t for t in parser.tables if t and symbol_label in t[0] and name_label in t[0]]
    if len(tables) != 1:
        raise SecConfigError(f"Cannot identify one complete {index} constituent table")
    header, *rows = tables[0]
    si, ni = header.index(symbol_label), header.index(name_label)
    members = []
    for row in rows:
        if len(row) <= max(si, ni) or not row[ni]:
            raise SecConfigError(f"Invalid {index} constituent row")
        members.append({"symbol": normalize_symbol(row[si]), "name": row[ni]})
    minimum, maximum = (500, 520) if index == "sp500" else (100, 110)
    if (not minimum <= len(members) <= maximum
            or len({m["symbol"] for m in members}) != len(members)):
        raise SecConfigError(f"Unexpected {index} constituent count or duplicate symbols")
    return members


def parse_sec_tickers(payload):
    """Use the SEC company_tickers.json schema; conflicting mappings fail."""
    try:
        if not isinstance(payload, dict) or not payload:
            raise ValueError()
        result = {}
        for item in payload.values():
            symbol = normalize_symbol(item["ticker"])
            cik = str(item["cik_str"])
            if not re.fullmatch(r"[0-9]{1,10}", cik) or int(cik) == 0:
                raise ValueError()
            cik = cik.zfill(10)
            if symbol in result and result[symbol] != cik:
                raise ValueError()
            result[symbol] = cik
        return result
    except (TypeError, KeyError, ValueError):
        raise SecConfigError("Invalid or conflicting SEC ticker-to-CIK mapping") from None


def build_universe(sp500_html, nasdaq100_html, ticker_payload=None, *, fetched_at=None):
    """Create a provenance-bearing snapshot; unmapped symbols remain explicit."""
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat()
    mapping = parse_sec_tickers(ticker_payload) if ticker_payload is not None else {}
    members, sources, counts = {}, [], {}
    for index, body in (("sp500", sp500_html), ("nasdaq100", nasdaq100_html)):
        rows = parse_constituents(body, index)
        counts[index] = len(rows)
        for row in rows:
            symbol = row["symbol"]
            if symbol not in members:
                members[symbol] = dict(row, cik=mapping.get(symbol), indices=[])
            members[symbol]["indices"].append(index)
        sources.append({
            "index": index, "url": MEMBERSHIP_URLS[index],
            "label": "Wikipedia public constituent table (unofficial)",
            "retrieved_at": timestamp, "sha256": sha256(body).hexdigest(),
        })
    rows = sorted(members.values(), key=lambda m: m["symbol"])
    missing = [m["symbol"] for m in rows if not m["cik"]]
    counts.update(symbols=len(rows), companies=len({m["cik"] for m in rows if m["cik"]}))
    return {
        "schema_version": 1, "fetched_at": timestamp,
        "membership_note": "Retrieved public tables; this is not an index-provider-verified effective-date list. Refresh before use and after index changes.",
        "sources": sources, "mapping_source": SEC_TICKERS_URL if mapping else None,
        "mapping_status": "pending" if missing else "complete",
        "unmapped_symbols": missing, "counts": counts, "members": rows,
    }


def load_universe(path=DEFAULT_UNIVERSE):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        _validate_universe(data)
        return data
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise SecConfigError("Invalid or missing SEC universe; run --refresh-sec-universe") from None


def _validate_universe(data):
    if data["schema_version"] != 1 or data["mapping_status"] not in {"complete", "pending"}:
        raise SecConfigError("Invalid SEC universe")
    timestamp = datetime.fromisoformat(data["fetched_at"])
    if timestamp.tzinfo is None:
        raise SecConfigError("Universe date must include a timezone")
    members = data["members"]
    if not isinstance(members, list) or not members:
        raise SecConfigError("Empty SEC universe")
    symbols = set()
    for member in members:
        symbol = normalize_symbol(member["symbol"])
        if symbol in symbols or symbol != member["symbol"] or not isinstance(member["name"], str):
            raise SecConfigError("Invalid or duplicate universe symbol")
        symbols.add(symbol)
        if (not isinstance(member["indices"], list) or not member["indices"]
                or set(member["indices"]) - MEMBERSHIP_URLS.keys()):
            raise SecConfigError("Invalid universe index")
        if member["cik"] is not None and (not isinstance(member["cik"], str)
                or not re.fullmatch(r"[0-9]{10}", member["cik"]) or int(member["cik"]) == 0):
            raise SecConfigError("Invalid universe CIK")
    counts = {index: sum(index in m["indices"] for m in members) for index in MEMBERSHIP_URLS}
    counts.update(symbols=len(members), companies=len({m["cik"] for m in members if m["cik"]}))
    if (not 500 <= counts["sp500"] <= 520 or not 100 <= counts["nasdaq100"] <= 110
            or counts != data["counts"]):
        raise SecConfigError("SEC universe member counts are incomplete or inconsistent")
    missing = sorted(m["symbol"] for m in members if not m["cik"])
    if missing != sorted(data["unmapped_symbols"]):
        raise SecConfigError("Universe mapping status is inconsistent")
    if data["mapping_status"] == "complete" and (missing or data["mapping_source"] != SEC_TICKERS_URL):
        raise SecConfigError("Universe requires a complete official SEC CIK mapping")


class _Downloader:
    """One sequential client; SEC requests spaced at >=0.2 s (at most 5/s)."""

    def __init__(self, session, user_agent, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.session, self.user_agent, self.timeout = session, user_agent, timeout
        self.last_sec_request = None

    def get(self, url):
        sec = urlsplit(url).hostname == "www.sec.gov"
        if sec and self.last_sec_request is not None:
            time.sleep(max(0, 0.2 - (time.monotonic() - self.last_sec_request)))
        if sec:
            self.last_sec_request = time.monotonic()
        response = None
        try:
            response = self.session.get(
                url, headers={"User-Agent": self.user_agent if sec else USER_AGENT,
                              "Accept": "application/atom+xml, application/json, text/html"},
                timeout=self.timeout, stream=True, allow_redirects=False,
            )
            if response.status_code != 200:
                raise FeedFetchError(f"HTTP {response.status_code}")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise FeedFetchError("Response exceeds the 5 MiB limit")
                chunks.append(chunk)
            return b"".join(chunks)
        except requests.RequestException:
            raise FeedFetchError("SEC/universe network request failed") from None
        finally:
            if response is not None:
                response.close()


def refresh_universe(path=DEFAULT_UNIVERSE, *, user_agent=None, timeout=15, session=None):
    """Refresh all members and mappings atomically; never replace with a partial list."""
    agent = sec_user_agent(user_agent)
    client = session if session is not None else requests.Session()
    try:
        downloader = _Downloader(client, agent, timeout)
        sp500 = downloader.get(MEMBERSHIP_URLS["sp500"])
        nasdaq = downloader.get(MEMBERSHIP_URLS["nasdaq100"])
        try:
            tickers = json.loads(downloader.get(SEC_TICKERS_URL))
        except (ValueError, UnicodeError):
            raise SecConfigError("SEC ticker map is not valid JSON") from None
        data = build_universe(sp500, nasdaq, tickers)
        if data["unmapped_symbols"]:
            raise SecConfigError("SEC CIK mapping incomplete; unresolved: " + ", ".join(data["unmapped_symbols"]))
        _validate_universe(data)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return data
    finally:
        if session is None:
            client.close()


def sec_feed_url(form, start=0):
    if form not in SEC_FORMS or not isinstance(start, int) or start < 0:
        raise SecConfigError("Invalid SEC filing query")
    return "https://www.sec.gov/cgi-bin/browse-edgar?" + urlencode({
        "action": "getcurrent", "type": form, "owner": "exclude",
        "count": PAGE_SIZE, "start": start, "output": "atom",
    })


def parse_sec_filings(body, form, ciks):
    """Filter by both form and archive-path CIK, including amendments.

    Returns (matching Articles, raw entry count). A full page with zero matches
    still needs pagination. CIK disagreement in the title rejects the notice.
    """
    source = Source("sec_" + form.lower().replace("-", "_"),
                    "SEC EDGAR — S&P 500 + Nasdaq-100", sec_feed_url(form),
                    "official", "US", "atom")
    articles = parse_feed(source, body)
    raw_count = len(feedparser.parse(body).entries)
    matched = []
    for article in articles:
        title_form = re.match(r"^([A-Z0-9/-]+)\s+-\s+", article.title)
        if not title_form or title_form.group(1) not in {form, form + "/A"}:
            continue
        url = urlsplit(article.url)
        match = re.match(r"^/Archives/edgar/data/(\d{1,10})/", url.path)
        if url.scheme != "https" or url.hostname != "www.sec.gov" or not match:
            continue
        cik = match.group(1).zfill(10)
        title_cik = re.search(r"\((\d{1,10})\)\s*\(Filer\)", article.title, re.I)
        if cik not in ciks or (title_cik and title_cik.group(1).zfill(10) != cik):
            continue
        matched.append(Article(
            article.source_id, article.source_name, article.source_kind, article.region,
            article.title, article.url, article.published_at, article.summary,
            "", "filing_notice", article.external_id or article.url,
        ))
    return matched, raw_count


def collect_sec_filings(universe, *, user_agent=None, max_pages=10, timeout=15, session=None):
    """Fetch five form feeds, page before CIK filtering, and report coverage limits.

    Current feeds have a finite SEC retention window. This is not a historical
    backfill or a guarantee of complete coverage between infrequent runs.
    """
    agent = sec_user_agent(user_agent)
    _validate_universe(universe)
    if universe["mapping_status"] != "complete":
        raise SecConfigError("SEC universe has unmapped members; run --refresh-sec-universe")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(universe["fetched_at"])
    if age.total_seconds() < -86400 or age.total_seconds() > 7 * 86400:
        raise SecConfigError("SEC membership snapshot is stale; run --refresh-sec-universe")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be between 1 and 100")
    ciks = {m["cik"] for m in universe["members"]}
    client = session if session is not None else requests.Session()
    articles, diagnostics, seen = [], [], set()
    try:
        downloader = _Downloader(client, agent, timeout)
        for form in SEC_FORMS:
            info = {"source_id": "sec_" + form.lower().replace("-", "_"), "form": form,
                    "pages": 0, "scanned": 0, "matched": 0, "status": "ok",
                    "window_exhausted": False, "warnings": []}
            page_hashes = set()
            for page in range(max_pages):
                try:
                    body = downloader.get(sec_feed_url(form, page * PAGE_SIZE))
                    matched, count = parse_sec_filings(body, form, ciks)
                except (FeedFetchError, SecConfigError) as error:
                    info.update(status="error", error=str(error))
                    break
                info["pages"] += 1
                digest = sha256(body).hexdigest()
                if digest in page_hashes:
                    info["status"] = "partial"
                    info["warnings"].append("Repeated SEC page; pagination stopped before claiming complete coverage")
                    break
                page_hashes.add(digest)
                info["scanned"] += count
                for article in matched:
                    key = article.external_id or article.url
                    if key not in seen:
                        articles.append(article)
                        seen.add(key)
                        info["matched"] += 1
                if count < PAGE_SIZE:
                    info["window_exhausted"] = True
                    break
            else:
                info["status"] = "partial"
                info["warnings"].append("SEC page limit reached; older filings may be missing")
            diagnostics.append(info)
            if info.get("error") in {"HTTP 403", "HTTP 429"}:
                # Respect fair-access blocks/rate limits rather than continuing
                # to hit the remaining form feeds or changing identity.
                diagnostics.append({"source_id": "sec", "status": "error",
                                    "error": "SEC access limited; remaining feeds skipped"})
                break
    finally:
        if session is None:
            client.close()
    return articles, diagnostics
