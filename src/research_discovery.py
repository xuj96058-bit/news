"""Bounded, gap-directed discovery from real public links and known directories.

This is not an open-web search engine. URLs come from retained source material,
links on those pages, an explicit catalog, or validated SEC filing metadata.
Downloaded text is evidence to inspect, never a claim of independent verification.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit

from .research import MAX_SOURCES, MAX_TOTAL_SOURCE_CHARS, ResearchError, prepare_evidence
from .research_evidence import CATALOG
from .research_quality import _contains, _normal, rank_articles, select_relevant_passages
from .research_retrieval import MAX_EXCERPT_CHARS, PUBLIC_HOSTS, RetrievalError, _capture, _extract, _safe_url
from .sec_universe import DEFAULT_UNIVERSE, SecConfigError, sec_user_agent


_SECONDARY_HOSTS = {"www.cnbc.com", "www.bbc.com", "www.bbc.co.uk", "news.rthk.hk",
                    "rthk.hk", "www.rthk.hk", "www.ft.com"}
_PRIMARY_HOSTS = PUBLIC_HOSTS - _SECONDARY_HOSTS
_FINANCIAL_TERMS = ("cash flow", "capex", "capital expenditure", "capital spending", "earnings",
                    "revenue", "profit", "valuation", "financial statements", "balance sheet",
                    "现金流", "资本开支", "资本支出", "财报", "营收", "盈利", "估值", "利润")
_CATALOG_TERMS = {
    "finra": ("margin debt", "finra", "保证金债务", "证券保证金", "融资余额"),
    "cftc": ("cftc", "futures positioning", "期货仓位", "期货持仓"),
    "scoos": ("scoos", "dealer financing", "交易商融资"),
    "sec_pf": ("private fund", "私募基金"),
    "sector_pack": ("sector positioning", "sector crowding", "行业持仓", "行业拥挤"),
    "spy": ("s&p 500 weights", "sp500 weights", "标普500权重"),
}
_COMPANY_ALIASES = {"MSFT": ("microsoft", "微软"), "AAPL": ("apple", "苹果"),
                    "AMZN": ("amazon", "亚马逊"), "GOOGL": ("alphabet", "google", "谷歌"),
                    "META": ("meta",), "NVDA": ("nvidia", "英伟达"), "TSLA": ("tesla", "特斯拉")}
_URL = re.compile(r"https://[^\s<>\"'\]\[）]+")


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.publication_dates, self.modified_dates, self.title_parts = [], [], [], []
        self._anchor = None
        self._title = False
        self._json = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self._anchor = [attrs["href"], ""]
        if tag == "title":
            self._title = True
        if tag == "meta" and (attrs.get("property") or attrs.get("name") or attrs.get("itemprop", "")).lower() in {
                "article:published_time", "datepublished", "citation_publication_date", "dc.date.issued"}:
            self.publication_dates.append(attrs.get("content", ""))
        if tag == "meta" and (attrs.get("property") or attrs.get("name") or attrs.get("itemprop", "")).lower() in {
                "article:modified_time", "datemodified", "dc.date.modified"}:
            self.modified_dates.append(attrs.get("content", ""))
        if tag == "time" and attrs.get("itemprop", "").lower() == "datepublished":
            self.publication_dates.append(attrs.get("datetime", ""))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._json = []

    def handle_data(self, value):
        if self._anchor is not None:
            self._anchor[1] += value
        if self._title:
            self.title_parts.append(value)
        if self._json is not None:
            self._json.append(value)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self.links.append(tuple(self._anchor))
            self._anchor = None
        if tag == "title":
            self._title = False
        if tag == "script" and self._json is not None:
            try:
                value = json.loads("".join(self._json))
                nodes = value if isinstance(value, list) else [value]
                for node in nodes[:30]:
                    if isinstance(node, dict):
                        self.publication_dates.append(node.get("datePublished"))
                        self.modified_dates.append(node.get("dateModified"))
                        for entry in node.get("@graph", [])[:30]:
                            if isinstance(entry, dict):
                                self.publication_dates.append(entry.get("datePublished"))
                                self.modified_dates.append(entry.get("dateModified"))
            except (ValueError, TypeError):
                pass
            self._json = None


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", value):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return None


def _requirements(requirements):
    if not isinstance(requirements, (list, tuple)):
        raise ValueError("requirements must be a list")
    output = []
    for index, item in enumerate(requirements[:30], 1):
        if isinstance(item, str):
            output.append({"id": f"R{index}", "text": item[:2000]})
        elif isinstance(item, dict):
            parts = [item.get(field, "") for field in ("description", "title", "label", "question", "metric")]
            for field in ("keywords", "entities"):
                values = item.get(field)
                if isinstance(values, (list, tuple)):
                    parts += [part for part in values if isinstance(part, str)]
            output.append({"id": str(item.get("id") or item.get("requirement_id") or f"R{index}")[:80],
                           "text": " ".join(part for part in parts if isinstance(part, str))[:2000]})
        else:
            raise ValueError("requirements must contain strings or dictionaries")
    return output


def _matching_ids(candidate, requirements, topic):
    matches = [item["id"] for item in requirements if item["text"] and
               rank_articles([candidate], item["text"], deduplicate=False)]
    if not matches and rank_articles([candidate], topic, deduplicate=False):
        matches = [item["id"] for item in requirements]
    return matches


def _source_links(source):
    for field in ("original_url", "document_url", "primary_url"):
        if isinstance(source.get(field), str):
            yield source[field], "explicit_original_link"
    for match in _URL.finditer(source.get("content") or ""):
        yield match.group().rstrip(".,;:!?)。，；：！"), "source_body_link"
    for origin in source.get("origins") or []:
        if isinstance(origin, dict) and origin.get("url") != source.get("url"):
            yield origin.get("url"), "source_origin_link"
    scope = source.get("content_scope", "")
    already_read = source.get("retrieval", {}).get("status") == "ok" or scope in {
        "full_text", "article_full_text", "html_full_text", "pdf_full_text"}
    if source.get("url") and not already_read:
        yield source["url"], "source_page"


def _companies(topic, requirements):
    text = _normal(topic + " " + " ".join(item["text"] for item in requirements))
    if not any(_contains(text, term) for term in _FINANCIAL_TERMS):
        return []
    try:
        members = json.loads(DEFAULT_UNIVERSE.read_text(encoding="utf-8"))["members"]
    except (OSError, ValueError, KeyError):
        return []
    found, ciks = [], set()
    for member in members:
        symbol, name, cik = member.get("symbol", ""), member.get("name", ""), str(member.get("cik", ""))
        name = re.sub(r",?\s+(?:inc\.?|corp\.?|corporation|company|plc|ltd\.?)$", "", name, flags=re.I)
        terms = [name, *_COMPANY_ALIASES.get(symbol, ())]
        # Avoid single-letter ticker symbols matching ordinary prose.
        if len(symbol) >= 2:
            terms.append(symbol)
        if re.fullmatch(r"\d{10}", cik) and cik not in ciks and any(_contains(text, _normal(term)) for term in terms if term):
            found.append({"symbol": symbol, "name": name, "cik": cik})
            ciks.add(cik)
    return found[:2]


def _sec_filings(body, candidate, cutoff):
    try:
        document = json.loads(body)
        if int(document.get("cik", -1)) != int(candidate["company"]["cik"]):
            raise ValueError()
        recent = document["filings"]["recent"]
        rows = zip(recent["form"], recent["filingDate"], recent["accessionNumber"], recent["primaryDocument"])
        matches = []
        for form, publication, accession, filename in rows:
            published = _day(publication)
            if form not in {"10-Q", "10-K"} or not published or published > cutoff:
                continue
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:htm|html)", filename):
                continue
            # SEC's directory contract uses these validated API-returned fields.
            url = f"https://www.sec.gov/Archives/edgar/data/{int(candidate['company']['cik'])}/{accession.replace('-', '')}/{filename}"
            matches.append({**candidate, "url": url, "kind": "document", "publication_date": published.isoformat(),
                            "title": f"{candidate['company']['name']} SEC {form} filed {published.isoformat()}",
                            "reason": "sec_filing_metadata", "priority": 1})
        return sorted(matches, key=lambda item: item["publication_date"], reverse=True)[:2]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RetrievalError("invalid_sec_metadata") from None


def supplement_sources(sources, *, topic, requirements, as_of, attempted_urls=(), max_fetches=4,
                       deadline=None, get=None):
    """Append prepared S-numbered snapshots; preserve original sources on failure.

    ``deadline`` is an absolute monotonic time. ``max_fetches`` counts documents,
    including directory reads; each capture has at most four checked HTTP hops.
    Missing publication dates remain unknown; later publications are excluded.
    SEC directory discovery needs the existing SEC_USER_AGENT contact setting.
    """
    if not isinstance(sources, (list, tuple)) or any(not isinstance(item, dict) for item in sources):
        raise ValueError("sources must contain prepared source dictionaries")
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 500:
        raise ValueError("topic must be a nonempty string of at most 500 characters")
    if isinstance(max_fetches, bool) or not isinstance(max_fetches, int) or not 0 <= max_fetches <= 12:
        raise ValueError("max_fetches must be between 0 and 12")
    cutoff = _day(as_of)
    if cutoff is None:
        raise ValueError("as_of must be an ISO calendar date")
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise ValueError("deadline must be a finite monotonic timestamp")
    requirements = _requirements(requirements)
    result, outcomes, attempted, queued, queue = deepcopy(list(sources)), [], [], set(), []
    for url in attempted_urls:
        try:
            url = _safe_url(url, resolve=False)
            if url not in attempted:
                attempted.append(url)
        except RetrievalError:
            pass

    def enqueue(url, *, reason, requirement_ids, title="", parent_ids=(), priority=2, depth=0, **extra):
        try:
            canonical = _safe_url(url, resolve=False)
        except RetrievalError as error:
            outcomes.append({"status": "skipped", "reason": error.reason, "requirement_ids": requirement_ids,
                             "why": reason, "parent_source_ids": list(parent_ids)})
            return
        if canonical in queued or canonical in attempted:
            return
        queued.add(canonical)
        queue.append({"url": canonical, "reason": reason, "requirement_ids": requirement_ids,
                      "title": title, "parent_source_ids": list(parent_ids), "priority": priority,
                      "depth": depth, "kind": "document", **extra})

    if requirements:
        for source in sources:
            ids = _matching_ids(source, requirements, topic)
            if ids:
                for url, reason in list(_source_links(source))[:20]:
                    try:
                        host = urlsplit(url).hostname if isinstance(url, str) else None
                    except ValueError:
                        host = None
                    enqueue(url, reason=reason, requirement_ids=ids, title=source.get("title", ""),
                            parent_ids=[source.get("id")], priority=0 if reason != "source_page" and host in _PRIMARY_HOSTS else 2)
        combined = _normal(topic + " " + " ".join(item["text"] for item in requirements))
        for key, terms in _CATALOG_TERMS.items():
            if any(_contains(combined, term) for term in terms):
                title, url = CATALOG[key]
                enqueue(url, reason="relevant_public_catalog", requirement_ids=[item["id"] for item in requirements],
                        title=title, priority=4)
        for company in _companies(topic, requirements):
            enqueue(f"https://data.sec.gov/submissions/CIK{company['cik']}.json", reason="company_financial_gap",
                    requirement_ids=[item["id"] for item in requirements], title=company["name"],
                    priority=3, kind="sec_directory", company=company)

    fetches, added = 0, 0
    while queue:
        queue.sort(key=lambda item: item["priority"])
        candidate = queue.pop(0)
        outcome = {"url": candidate["url"], "requirement_ids": candidate["requirement_ids"],
                   "why": candidate["reason"], "parent_source_ids": candidate["parent_source_ids"]}
        reason = ("deadline" if deadline is not None and time.monotonic() >= deadline else
                  "fetch_budget" if fetches >= max_fetches else "source_budget" if len(result) >= MAX_SOURCES or
                  sum(len(item.get("content") or "") for item in result) >= MAX_TOTAL_SOURCE_CHARS else None)
        if reason:
            outcomes.append({**outcome, "status": "skipped", "reason": reason})
            continue
        try:
            user_agent = None
            if urlsplit(candidate["url"]).hostname in {"data.sec.gov", "www.sec.gov"}:
                try:
                    user_agent = sec_user_agent()
                except SecConfigError:
                    raise RetrievalError("sec_contact_not_configured") from None
            attempted.append(candidate["url"])
            fetches += 1
            body, content_type, final_url = _capture(candidate["url"], get, deadline=deadline, user_agent=user_agent)
            if final_url not in attempted:
                attempted.append(final_url)
            if deadline is not None and time.monotonic() >= deadline:
                raise RetrievalError("deadline")
            if candidate["kind"] == "sec_directory":
                filings = _sec_filings(body, candidate, cutoff)
                for filing in filings:
                    enqueue(filing.pop("url"), **{key: value for key, value in filing.items() if key != "parent_source_ids"},
                            parent_ids=candidate["parent_source_ids"])
                outcomes.append({**outcome, "status": "directory_read", "reason": "filings_found" if filings else "no_eligible_recent_filings",
                                 "document_sha256": sha256(body).hexdigest(), "discovered_count": len(filings)})
                continue
            text, kind = _extract(body, content_type)
            if deadline is not None and time.monotonic() >= deadline:
                raise RetrievalError("deadline")
            metadata = _Metadata()
            if kind == "html":
                metadata.feed(body.decode("utf-8", errors="replace"))
            dates = [_day(value) for value in metadata.publication_dates]
            dates = [value for value in dates if value]
            known_date = _day(candidate.get("publication_date"))
            if known_date:
                dates.append(known_date)
            if any(value > cutoff for value in dates):
                raise RetrievalError("published_after_as_of")
            modified = [_day(value) for value in metadata.modified_dates]
            modified = [value for value in modified if value]
            if any(value > cutoff for value in modified):
                raise RetrievalError("modified_after_as_of")
            # Conflicting metadata is not silently resolved to a convenient date.
            publication = dates[0].isoformat() if dates and len(set(dates)) == 1 else None
            query = topic + " " + " ".join(item["text"] for item in requirements if item["id"] in candidate["requirement_ids"])
            if candidate["depth"] < 1:
                for href, label in metadata.links[:120]:
                    url = urljoin(final_url, href)
                    try:
                        primary = urlsplit(url).hostname in _PRIMARY_HOSTS
                    except ValueError:
                        primary = False
                    if primary and label.strip() and rank_articles([{"title": label}], query, deduplicate=False):
                        enqueue(url, reason="linked_primary_document", requirement_ids=candidate["requirement_ids"],
                                title=label, parent_ids=candidate["parent_source_ids"], priority=1, depth=1)
            allowance = min(MAX_EXCERPT_CHARS, MAX_TOTAL_SOURCE_CHARS - sum(len(item.get("content") or "") for item in result))
            passages = select_relevant_passages(text, query, max_chars=allowance)
            if len(text) < 80 or not passages["content"]:
                raise RetrievalError("no_relevant_text")
            digest = sha256(body).hexdigest()
            if any(item.get("retrieval", {}).get("document_sha256") == digest for item in result):
                outcomes.append({**outcome, "status": "skipped", "reason": "duplicate_document"})
                continue
            primary = urlsplit(final_url).hostname in _PRIMARY_HOSTS
            title = " ".join(metadata.title_parts).strip() or candidate["title"] or "Public source capture"
            evidence = {"title": title, "publisher": urlsplit(final_url).hostname, "url": final_url,
                        "publication_date": publication, "source_type": "official" if primary else "manual",
                        "content": passages["content"]}
            # prepare_evidence validates the new snapshot and allocates its ID.
            # Keep originals, including custom lineage fields, byte-for-byte intact.
            prepared = prepare_evidence(result, [evidence])[-1]
            prepared.update({"content_scope": kind + "_excerpt", "content_truncated": passages["content_truncated"],
                             "supplied_character_count": len(text), "included_character_count": len(prepared["content"]),
                             "retrieval": {"status": "ok", "document_type": kind, "document_sha256": digest,
                                           "extracted_text_sha256": sha256(text.encode("utf-8")).hexdigest(),
                                           "selection_spans": passages["spans"], "captured_at": datetime.now(timezone.utc).isoformat(),
                                           "publication_date_basis": "sec_filing_metadata" if known_date else "document_metadata" if publication else "unknown",
                                           "document_modified_date": max(modified).isoformat() if modified else None,
                                           "as_of_status": "eligible" if publication else "publication_unknown",
                                           "requirement_ids": candidate["requirement_ids"], "parent_source_ids": candidate["parent_source_ids"],
                                           "discovery_method": candidate["reason"]}})
            result.append(prepared)
            added += 1
            outcomes.append({**outcome, "status": "added", "source_id": prepared["id"], "publication_date": publication,
                             "publication_status": "known" if publication else "unknown"})
        except (RetrievalError, ResearchError) as error:
            outcomes.append({**outcome, "status": "error", "reason": error.reason})
        except Exception:
            outcomes.append({**outcome, "status": "error", "reason": "discovery_failed"})
    if not outcomes:
        outcomes.append({"status": "skipped", "reason": "no_known_public_links", "requirement_ids": [item["id"] for item in requirements],
                         "why": "Known original links and explicit public directories only; open-web search is not configured."})
    return {"sources": result, "outcomes": outcomes, "attempted_urls": attempted, "added_count": added}
