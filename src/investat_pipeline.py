"""Collect Investat research into the shared inbox without invoking an LLM."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from .investat import InvestatClient, InvestatError


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(record, *keys) -> str:
    return next((_text(record.get(key)) for key in keys if _text(record.get(key))), "")


def _body(record) -> str:
    # Use text fields actually returned by the tool; never fetch article pages.
    for key in ("body", "bodyText", "content", "text"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _institution(record) -> str:
    value = record.get("institution")
    if isinstance(value, dict):
        value = value.get("name") or value.get("displayName")
    return _text(value) or _first_text(record, "institutionName", "institution_name")


def _url(record) -> str:
    """Keep a genuine supplied HTTP(S) link or leave the article link empty."""
    for key in ("url", "sourceUrl", "source_url"):
        value = _text(record.get(key))
        if not value:
            continue
        try:
            parts = urlsplit(value)
            if (parts.scheme.lower() in {"http", "https"} and parts.hostname
                    and parts.username is None and parts.password is None):
                # Accessing port also rejects malformed port numbers.
                parts.port
                return value
        except ValueError:
            pass
    return ""


def _normalize(record, article_id, body: str, *, partial=False) -> dict:
    title = _first_text(record, "title", "headline")
    summary = _first_text(record, "summary", "abstract", "excerpt")
    institution = _institution(record)
    return {
        "source_id": "investat",
        "source_name": "Investat" + (f" / {institution}" if institution else ""),
        "source_kind": "research",
        "region": "global",
        "external_id": str(article_id),
        "title": title,
        "summary": summary,
        "content": body,
        "url": _url(record),
        "published_at": _first_text(record, "publishedAt", "published_at") or None,
        "content_scope": ("partial_text" if partial else "provided_text") if body
                         else "summary" if summary and summary != title else "title_only",
    }


def _safe_error(exc, fallback="Investat request failed") -> str:
    # Provider payloads and arbitrary exception messages may contain credentials.
    if isinstance(exc, InvestatError):
        return str(InvestatError(exc.category, status_code=exc.status_code, rpc_code=exc.rpc_code))
    return fallback


def collect_investat(store, *, token=None, limit=20, max_body_pages=5,
                     timeout=15, client_factory=None):
    """Read summaries and bounded detail chunks, saving partial successes.

    Only an explicit nextBodyOffset advances the body cursor. The normalized
    allowlist passed to NewsStore excludes provider debug, session, and tool
    metadata. Missing article URLs remain empty; the store supplies identity.
    """
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50
            or isinstance(max_body_pages, bool) or not isinstance(max_body_pages, int)
            or max_body_pages < 1 or timeout <= 0):
        raise ValueError("Investat limit must be 1–50; body pages and timeout must be positive")
    report = {"fetched_at": datetime.now(timezone.utc).isoformat(), "added": 0,
              "updated": 0, "duplicates": 0, "failed_sources": 0, "sources": []}
    outcome = {"id": "investat", "name": "Investat", "kind": "research",
               "fetched": 0, "selected": 0, "body_pages": 0, "partial_articles": 0,
               "skipped": 0, "details_skipped": 0, "diagnostics": []}
    report["sources"].append(outcome)
    factory = client_factory or InvestatClient
    try:
        client = factory(token=token, timeout=timeout)
        items = client.list_articles(limit=limit)
        if not isinstance(items, list):
            raise InvestatError("payload")
    except Exception as exc:
        outcome.update(status="error", error=_safe_error(exc))
        report["failed_sources"] = 1
        report["database"] = store.stats()
        return report

    outcome["fetched"] = len(items)
    outcome["selected"] = min(len(items), limit)
    counts = {"added": 0, "updated": 0, "duplicates": 0}
    failures = False
    detail_blocked = False
    for item in items[:limit]:
        if not isinstance(item, dict):
            outcome["skipped"] += 1
            failures = True
            outcome["diagnostics"].append({"stage": "article", "error": "Invalid Investat article metadata"})
            continue
        article_id = item.get("id")
        if (isinstance(article_id, bool) or not isinstance(article_id, (str, int))
                or not str(article_id).strip()):
            outcome["skipped"] += 1
            failures = True
            outcome["diagnostics"].append({"stage": "article", "error": "Investat article ID is missing"})
            continue
        combined = dict(item)
        chunks = []
        partial = detail_blocked
        if detail_blocked:
            outcome["details_skipped"] += 1
        offset = 0
        seen_offsets = {offset}
        for page in range(0 if detail_blocked else max_body_pages):
            try:
                detail = client.get_article(article_id, body_offset=offset)
                if not isinstance(detail, dict) or not detail:
                    raise InvestatError("payload")
                detail_id = detail.get("id")
                if detail_id is not None and str(detail_id) != str(article_id):
                    raise InvestatError("payload")
                outcome["body_pages"] += 1
                # Empty fields on a chunk do not erase list-summary metadata.
                for key in ("title", "headline", "summary", "abstract", "excerpt",
                            "publishedAt", "published_at", "url", "sourceUrl", "source_url",
                            "institutionName", "institution_name"):
                    if _text(detail.get(key)):
                        combined[key] = detail[key]
                if _institution(detail):
                    combined["institution"] = _institution(detail)
                chunk = _body(detail)
                if chunk:
                    chunks.append(chunk)
                next_offset = detail.get("nextBodyOffset")
                has_more = any(detail.get(key) is True for key in
                               ("hasMoreBody", "hasMore", "truncated", "bodyTruncated"))
                if next_offset is None:
                    if has_more:
                        partial = True
                        outcome["diagnostics"].append({"stage": "body", "error": "Investat body cursor is missing"})
                    break
                if (isinstance(next_offset, bool) or not isinstance(next_offset, int)
                        or next_offset <= offset or next_offset in seen_offsets):
                    partial = True
                    outcome["diagnostics"].append({"stage": "body", "error": "Invalid Investat body cursor"})
                    break
                if page + 1 == max_body_pages:
                    partial = True
                    outcome["diagnostics"].append({"stage": "body", "error": "Investat body page limit reached"})
                    break
                seen_offsets.add(next_offset)
                offset = next_offset
            except Exception as exc:
                partial = True
                outcome["diagnostics"].append({"stage": "detail", "error": _safe_error(exc)})
                if isinstance(exc, InvestatError) and exc.status_code in {401, 403, 429}:
                    detail_blocked = True
                break
        body = "".join(chunks) if chunks else _body(item)
        article = _normalize(combined, article_id, body, partial=partial)
        article["url"] = article["url"] or _url(item)
        if partial:
            failures = True
            outcome["partial_articles"] += 1
        if not article["title"]:
            failures = True
            outcome["skipped"] += 1
            outcome["diagnostics"].append({"stage": "article", "error": "Investat article title is missing"})
            continue
        try:
            saved = store.ingest([article])
            for key in counts:
                counts[key] += saved[key]
        except Exception:
            failures = True
            outcome["skipped"] += 1
            outcome["diagnostics"].append({"stage": "save", "error": "Investat article could not be saved"})
    outcome.update(counts)
    report.update(counts)
    if failures:
        outcome.update(status="partial" if sum(counts.values()) else "error",
                       error="Some Investat articles or text could not be collected")
        report["failed_sources"] = 1
    else:
        outcome["status"] = "ok" if items else "empty"
    report["database"] = store.stats()
    return report
