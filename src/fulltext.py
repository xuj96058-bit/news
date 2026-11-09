"""Optional full-text extraction with Trafilatura.

Disabled by default. Enable with NEWS_FETCH_FULLTEXT=1 (and install trafilatura).
Does not change existing feed-only behaviour when the flag is off or the package is missing.
"""

from __future__ import annotations

import os
from typing import Any


def fulltext_enabled() -> bool:
    return os.environ.get("NEWS_FETCH_FULLTEXT", "").strip().lower() in {"1", "true", "yes", "on"}


def extract_fulltext(url: str, *, timeout: float = 15) -> str | None:
    """Return extracted main text, or None on any failure / missing dependency."""
    if not fulltext_enabled() or not url:
        return None
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        downloaded = trafilatura.fetch_url(url, timeout=timeout)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if text and len(text.strip()) > 80:
            return text.strip()
    except Exception:
        return None
    return None


def enrich_item(item: dict[str, Any], *, timeout: float = 15) -> dict[str, Any]:
    """If full-text is enabled and the item looks like a summary, try to fill content."""
    if not fulltext_enabled():
        return item
    url = item.get("url") or ""
    content = (item.get("content") or "").strip()
    summary = (item.get("summary") or "").strip()
    # Only enrich when we have a link but little or no body.
    if not url or (content and len(content) > 400):
        return item
    extracted = extract_fulltext(url, timeout=timeout)
    if extracted:
        item = dict(item)
        item["content"] = extracted
        item["content_scope"] = "fulltext_trafilatura"
        # Keep original summary for reference.
        if summary and summary not in extracted:
            item["summary"] = summary
    return item
