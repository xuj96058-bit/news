"""Bounded retrieval of selected public sources, independent of model output.

Known publisher/official hosts are an explicit allowlist. Every redirect and
resolved address is checked before access; environment credentials/proxies are
disabled. Captures are relevant excerpts, never independently verified facts.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from io import BytesIO
import ipaddress
import re
import socket
import time
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from .research_quality import _concept, _contains, _normal, rank_articles, select_relevant_passages


# Exact hosts prevent an arbitrary subdomain (or manually labelled 'official'
# URL) from granting access to local networks. Add publishers deliberately.
PUBLIC_HOSTS = frozenset({
    "www.federalreserve.gov", "www.finra.org", "www.sec.gov", "www.cftc.gov",
    "www.ssga.com", "www.bls.gov", "apps.bea.gov", "www.bea.gov", "www.ecb.europa.eu",
    "www.eia.gov", "www.stats.gov.cn", "www.pbc.gov.cn", "www.hkex.com.hk",
    "www.hkma.gov.hk", "api.hkma.gov.hk", "www.cnbc.com", "www.bbc.com", "www.bbc.co.uk",
    "news.rthk.hk", "rthk.hk", "www.rthk.hk", "www.ft.com", "nvidianews.nvidia.com",
    "www.apple.com", "www.cboe.com", "ir.aboutamazon.com", "investor.fb.com",
    "investor.atmeta.com", "investor.nvidia.com", "www.microsoft.com",
    "www.apollo.com", "www.ubs.com", "www.blackrock.com", "am.jpmorgan.com",
    "www.hkexnews.hk", "www1.hkexnews.hk",
})
MAX_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_CHARS = 250000
MAX_EXCERPT_CHARS = 12000
MAX_TOTAL_SOURCE_CHARS = 180000  # Same report budget as research.py.
MAX_PDF_PAGES = 80
FETCH_SECONDS = 30
_PRESERVE_SCOPES = {"provided_text", "official_evidence", "manual_evidence", "full_text",
                    "article_full_text", "html_full_text", "pdf_full_text"}
_AUTH_KEYS = {"key", "secret", "password", "pass", "auth", "authorization", "bearer", "sig",
              "token", "apikey", "signature", "credential", "credentials"}
_MARKET_SIGNALS = (
    "monetary policy", "interest rate", "interest rates", "treasury", "yields", "federal reserve",
    "central bank", "stocks", "stock", "shares", "equity", "equities", "bonds", "bond", "futures",
    "earnings", "inflation", "cpi", "gdp", "volatility", "crude oil", "oil prices", "oil supply",
    "capex", "capital expenditure", "capital spending", "revenue", "profits", "unemployment",
    "foreign exchange", "bank lending", "margin debt", "borrowing", "holdings",
    "利率", "通胀", "资本开支", "财报", "股市", "股票", "债券", "汇率", "央行", "原油",
    "融资", "持仓", "波动率", "金融市场",
)
_MARKET_TOPIC = ("inflation earnings monetary policy interest rates treasury yields stocks stock shares "
    "equity equities bonds bond futures volatility crude oil prices supply capex capital expenditure "
    "spending revenue profits unemployment foreign exchange bank lending margin debt borrowing holdings "
    "利率 通胀 资本开支 财报 股市 股票 债券 汇率 央行 原油 融资 持仓 波动率 金融市场")


def _daily_market_topic(topic):
    text = _normal(topic)
    return (any(_contains(text, marker) for marker in ("金融市场", "financial market", "financial markets"))
        and any(_contains(text, marker) for marker in ("今日", "每日", "daily", "briefing", "重要变化"))
        and not any(_concept(text, key) for key in ("crowding", "leverage", "positioning", "inflation", "earnings")))


def _financial_candidate(source):
    # A financial word in page-wide boilerplate cannot admit a travel/celebrity
    # story. Daily reports require a recognisable market subject in the headline.
    title = _normal(source.get("title", ""))
    return any(_contains(title, marker) for marker in _MARKET_SIGNALS)


class RetrievalError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _public_addresses(host, port=443):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise RetrievalError("unsafe_address")
    return addresses


class _PublicHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        # Validate and connect the SAME resolved addresses. A second hostname
        # lookup by the normal transport could otherwise permit DNS rebinding.
        if self._dns_host not in PUBLIC_HOSTS or self.port != 443:
            raise RetrievalError("unsafe_address")
        try:
            addresses = _public_addresses(self._dns_host, self.port)
        except OSError:
            raise NewConnectionError(self, "Public hostname could not be resolved") from None
        last_error = None
        for family, kind, protocol, _, address in addresses:
            connection = None
            try:
                connection = socket.socket(family, kind, protocol)
                connection.settimeout(self.timeout)
                for option in self.socket_options or ():
                    connection.setsockopt(*option)
                if self.source_address:
                    connection.bind(self.source_address)
                connection.connect(address)
                return connection
            except OSError as error:
                last_error = error
                if connection is not None:
                    connection.close()
        if isinstance(last_error, socket.timeout):
            raise ConnectTimeoutError(self, "Public source connection timed out") from None
        raise NewConnectionError(self, "Public source connection failed") from None


class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _PublicHTTPSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme, https=_PublicHTTPSPool)


def _safe_url(value, *, resolve=True):
    if not isinstance(value, str) or not value or len(value) > 2048 or re.search(r"[\s<>\\]", value):
        raise RetrievalError("unsafe_url")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != "https" or host not in PUBLIC_HOSTS or parts.username is not None
                or parts.password is not None or parts.port not in {None, 443}):
            raise RetrievalError("unsafe_url")
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _AUTH_KEYS or normalized.endswith(("token", "apikey", "secret", "password", "signature", "credential")):
                raise RetrievalError("credential_url")
        if resolve:
            _public_addresses(host)
        return urlunsplit(("https", parts.netloc.casefold(), parts.path or "/", parts.query, ""))
    except RetrievalError:
        raise
    except (ValueError, OSError):
        raise RetrievalError("unsafe_address") from None


class _ArticleDocument(HTMLParser):
    """Keep row/cell boundaries; prefer article/main to page navigation."""
    _HIDDEN = {"script", "style", "noscript", "nav", "footer", "aside", "form", "svg", "template"}
    _BLOCK = {"p", "div", "section", "li", "h1", "h2", "h3", "h4", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.article, self.main = [], [], []
        self.hidden, self.article_depth, self.main_depth = 0, 0, 0

    def _append(self, text):
        if self.hidden:
            return
        self.parts.append(text)
        if self.article_depth:
            self.article.append(text)
        if self.main_depth:
            self.main.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden += 1
        if tag == "article":
            self.article_depth += 1
        if tag == "main":
            self.main_depth += 1
        if tag in self._BLOCK or tag in {"table", "article", "main"}:
            self._append("\n\n")
        elif tag == "br":
            self._append("\n")
        elif tag in {"th", "td"}:
            self._append(" | ")

    def handle_endtag(self, tag):
        if tag in self._BLOCK or tag in {"table", "article", "main"}:
            self._append("\n\n")
        elif tag == "tr":
            self._append("\n")
        if tag in self._HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)

    def handle_data(self, data):
        self._append(data)

    @property
    def text(self):
        selected = self.article if any(part.strip() for part in self.article) else self.main if any(part.strip() for part in self.main) else self.parts
        value = "".join(selected)
        value = "\n".join(re.sub(r"[ \t\r]+", " ", line).strip() for line in value.splitlines())
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def _capture(url, get=None):
    response, client = None, None
    deadline = time.monotonic() + FETCH_SECONDS
    if get is None:
        client = requests.Session()
        client.trust_env = False  # No .netrc, ambient proxy, cookies or auth.
        client.mount("https://", _PublicHTTPSAdapter(max_retries=0))
        get = client.get
    try:
        current = url
        for hop in range(4):
            current = _safe_url(current)
            if time.monotonic() >= deadline:
                raise RetrievalError("timeout")
            response = get(current, headers={"User-Agent": "news-research-reader/2.0",
                "Accept": "text/html,application/pdf;q=0.9,text/plain;q=0.8"},
                timeout=(3, 8), allow_redirects=False, stream=True)
            status = response.status_code
            # Injected clients also cannot silently return an unchecked redirect.
            if getattr(response, "history", ()):
                raise RetrievalError("unsafe_redirect")
            actual = getattr(response, "url", None)
            if actual and _safe_url(actual) != current:
                raise RetrievalError("unsafe_redirect")
            if status in {301, 302, 303, 307, 308}:
                target = response.headers.get("Location")
                response.close()
                response = None
                if hop == 3 or not target:
                    raise RetrievalError("redirect_limit")
                current = _safe_url(urljoin(current, target))
                continue
            if status != 200:
                raise RetrievalError("http_error")
            declared = response.headers.get("Content-Length", "")
            if str(declared).isdigit() and int(declared) > MAX_BYTES:
                raise RetrievalError("size_limit")
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                if time.monotonic() >= deadline:
                    raise RetrievalError("timeout")
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RetrievalError("size_limit")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("Content-Type", ""), current
        raise RetrievalError("redirect_limit")
    except RetrievalError:
        raise
    except requests.Timeout:
        raise RetrievalError("timeout") from None
    except Exception:
        raise RetrievalError("fetch_failed") from None
    finally:
        if response is not None:
            response.close()
        if client is not None:
            client.close()


def _extract(body, content_type):
    if body.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(body))
            if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
                raise RetrievalError("pdf_limit")
            pages, chars, deadline = [], 0, time.monotonic() + FETCH_SECONDS
            for page in reader.pages:
                if time.monotonic() >= deadline:
                    raise RetrievalError("timeout")
                value = page.extract_text(extraction_mode="layout") or ""
                chars += len(value)
                if chars > MAX_DOCUMENT_CHARS:
                    raise RetrievalError("text_limit")
                pages.append(value)
            return "\n\n".join(pages).strip(), "pdf"
        except RetrievalError:
            raise
        except ImportError:
            raise RetrievalError("pdf_unavailable") from None
        except Exception:
            raise RetrievalError("extraction_failed") from None
    if "pdf" in content_type.casefold():
        raise RetrievalError("extraction_failed")
    if content_type and not any(kind in content_type.casefold() for kind in ("html", "text/plain", "xhtml")):
        raise RetrievalError("unsupported_format")
    encoding = re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", body[:4096], re.I)
    try:
        decoded = body.decode(encoding[1].decode("ascii") if encoding else "utf-8", errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    if "text/plain" in content_type.casefold():
        text, kind = decoded.strip(), "text"
    else:
        parser = _ArticleDocument()
        parser.feed(decoded)
        parser.close()
        text, kind = parser.text, "html"
    if len(text) > MAX_DOCUMENT_CHARS:
        raise RetrievalError("text_limit")
    return text, kind


def _enrich(source, topic, get, max_chars=MAX_EXCERPT_CHARS):
    original = deepcopy(source)
    identifier = original.get("id")
    try:
        body, content_type, url = _capture(original.get("url"), get)
        text, kind = _extract(body, content_type)
        if len(text) < 80:
            raise RetrievalError("insufficient_text")
        passages = select_relevant_passages(text, topic, max_chars=max_chars)
        excerpt = passages["content"]
        if not excerpt:
            raise RetrievalError("no_relevant_text")
        # A short snippet must not replace a longer supplied feed capture.
        if len(excerpt) < len(original.get("content") or ""):
            raise RetrievalError("shorter_capture")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        enriched = deepcopy(original)
        enriched.update({"original_snapshot": original, "content": excerpt, "url": url,
            "content_scope": kind + "_excerpt", "content_truncated": passages["content_truncated"],
            "supplied_character_count": len(text), "included_character_count": len(excerpt),
            "retrieved_at": now, "retrieved_date": datetime.fromisoformat(now).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            "verification_status": "not_independently_verified",
            "retrieval": {"status": "ok", "document_type": kind, "extracted_character_count": len(text),
                          "selection_spans": passages["spans"], "captured_at": now}})
        return enriched, {"source_id": identifier, "status": "ok", "content_scope": enriched["content_scope"],
                          "extracted_character_count": len(text), "included_character_count": len(excerpt)}
    except RetrievalError as error:
        original["retrieval"] = {"status": "error", "reason": error.reason}
        return original, {"source_id": identifier, "status": "error", "reason": error.reason}
    except Exception:
        original["retrieval"] = {"status": "error", "reason": "extraction_failed"}
        return original, {"source_id": identifier, "status": "error", "reason": "extraction_failed"}


def enrich_sources(sources, *, topic, max_fetches=6, get=None):
    """Return copied snapshots and safe outcomes in original S-ID order.

    ``get`` is a requests-compatible transport for tests; URL, DNS and redirect
    checks still apply. Existing supplied/full evidence is never overwritten.
    Failed captures leave original content, scope, dates and IDs intact.
    """
    if not isinstance(sources, (list, tuple)) or any(not isinstance(source, dict) for source in sources):
        raise ValueError("sources must contain prepared source dictionaries")
    if isinstance(max_fetches, bool) or not isinstance(max_fetches, int) or not 0 <= max_fetches <= 12:
        raise ValueError("max_fetches must be between 0 and 12")
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 500:
        raise ValueError("topic must be a nonempty string of at most 500 characters")
    result = deepcopy(list(sources))
    outcomes = [{"source_id": source.get("id"), "status": "preserved", "reason": "supplied_or_full_text"}
                if source.get("content_scope", "") in _PRESERVE_SCOPES or source.get("content_scope", "").endswith("_full_text")
                else {"source_id": source.get("id"), "status": "skipped", "reason": "unrelated"} for source in sources]
    eligible = []
    daily_market = _daily_market_topic(topic)
    ranking_topic = _MARKET_TOPIC if daily_market else topic
    candidates = [source for source in sources if _financial_candidate(source)] if daily_market else sources
    ranked = rank_articles(candidates, ranking_topic)
    for item in ranked:
        source = item["article"]
        index = next(index for index, candidate in enumerate(sources) if candidate is source)
        for duplicate in item.get("duplicate_articles", []):
            duplicate_index = next(index for index, candidate in enumerate(sources) if candidate is duplicate)
            if outcomes[duplicate_index]["status"] != "preserved":
                outcomes[duplicate_index] = {"source_id": duplicate.get("id"), "status": "skipped", "reason": "duplicate_event"}
        scope = source.get("content_scope", "")
        if scope in _PRESERVE_SCOPES or scope.endswith("_full_text"):
            outcomes[index] = {"source_id": source.get("id"), "status": "preserved", "reason": "supplied_or_full_text"}
            continue
        if not source.get("url"):
            outcomes[index] = {"source_id": source.get("id"), "status": "skipped", "reason": "missing_url"}
            continue
        if len(eligible) >= max_fetches:
            outcomes[index] = {"source_id": source.get("id"), "status": "skipped", "reason": "fetch_budget"}
            continue
        eligible.append(index)
    if eligible:
        supplied_chars = sum(len(source.get("content") or "") for source in sources)
        growth_allowance = max(0, MAX_TOTAL_SOURCE_CHARS - supplied_chars) // len(eligible)
        with ThreadPoolExecutor(max_workers=min(3, len(eligible))) as pool:
            jobs = [(index, pool.submit(_enrich, sources[index], ranking_topic, get,
                min(MAX_EXCERPT_CHARS, len(sources[index].get("content") or "") + growth_allowance))) for index in eligible]
            for index, job in jobs:
                result[index], outcomes[index] = job.result()
    return result, outcomes
