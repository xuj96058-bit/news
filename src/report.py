"""Hong Kong daily selection, a local digest, and an evidence-bound AI essay."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
import unicodedata

from .analysis_errors import ProviderConfigurationError
from .collector import load_events
from .analyzer import format_result, AnalysisResult
from .news_store import DEFAULT_DB_PATH, NewsStore, canonicalize_url
from .research import HK_TIMEZONE, ResearchError, _date_label, prepare_sources
from .research_workflow import generate_research_workflow


DAILY_TOPIC = "今日金融市場的重要變化與影響"
_DAILY_INSTRUCTIONS = """撰寫資料截至香港日期當日的金融市場論說文。先從原始新聞找出值得解釋的重要變化，
提出一個有證據支持的核心論點，以連貫段落比較不同事件、原因、成立條件和市場含義。
根據市場影響範圍與新增資訊選材；不按模型置信度排列，不逐則拼貼舊分析。
同一新聞的轉載來源不算獨立事件。發佈日只用來選取今日新聞，數據的實際觀察期間仍須分開標示。
只討論提供材料能支持的市場含義，不補寫未提供的行情、價格、報酬率或市場反應。
若資料不足以支持完整市場結論，縮小論點；不得把沒有來源的宏觀敘述填入正文。"""


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _published_at(record):
    """Legacy timestamp is save time, never evidence of publication time."""
    for field in ("published_at", "publication_date", "date"):
        value = record.get(field)
        if _date_label(value):
            return value.isoformat() if isinstance(value, datetime) else str(value)
    return None


def _legacy_event(record):
    event = dict(record)
    original = _text(record.get("news") or record.get("content") or record.get("summary"))
    event.update(
        published_at=_published_at(record),
        title=_text(record.get("title")) or original.split("\n", 1)[0][:180],
        summary=_text(record.get("summary")) or original,
        content=_text(record.get("content")),
        content_scope=record.get("content_scope") or "provided_text",
        source_url=record.get("source_url") or record.get("url"),
        original_text_source="manual",
    )
    return event


def _inbox_event(article):
    """Retain the original article alongside its separately stored analysis."""
    event = dict(article)
    origins = article.get("sources") or []
    event.update(source=", ".join(dict.fromkeys(
        _text(item.get("source_name") or item.get("publisher")) for item in origins
        if _text(item.get("source_name") or item.get("publisher")))))
    event["source"] = event["source"] or article.get("source_name") or "未提供出版者"
    event["source_url"] = article.get("url")
    event["published_at"] = _published_at(article)
    return event


def load_report_events():
    """Combine completed legacy and inbox analyses, preserving publication data."""
    events = [_legacy_event(event) for event in load_events() if isinstance(event, dict)]
    if DEFAULT_DB_PATH.exists():
        with NewsStore() as inbox:
            events.extend(_inbox_event(article) for article in inbox.export_articles()
                          if article.get("status") == "analyzed" and article.get("analysis"))
    return events


def _load_daily_articles():
    """The essay uses original text; an inbox analysis is not a prerequisite."""
    events = [_legacy_event(event) for event in load_events() if isinstance(event, dict)]
    if DEFAULT_DB_PATH.exists():
        with NewsStore() as inbox:
            events.extend(_inbox_event(article) for article in inbox.export_articles())
    return events


def _day(as_of):
    day = _date_label(as_of) if as_of is not None else datetime.now(HK_TIMEZONE).date().isoformat()
    if not day:
        raise ResearchError("invalid_dates")
    return day


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", _text(value)).casefold().split())


def _url_identity(event):
    try:
        return canonicalize_url(event.get("source_url") or event.get("url") or "")
    except (ValueError, TypeError):
        return None


def _origin(event):
    return {"source_id": event.get("source_id") or "legacy",
            "source_name": event.get("source_name") or event.get("source") or "未提供出版者",
            "title": event.get("title"), "url": event.get("source_url") or event.get("url"),
            "published_at": event.get("published_at"),
            "content_scope": event.get("content_scope") or "provided_text"}


def _deduplicate(events):
    """Merge canonical URLs or identical headline/body, retaining attribution.

    Differently worded reports remain separate evidence for the essay planner.
    A headline alone cannot establish that two stories are the same event.
    """
    parents, identities = list(range(len(events))), {}

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, event in enumerate(events):
        title = _normal(event.get("title"))
        body = _normal(event.get("content") or event.get("summary") or event.get("news"))
        keys = []
        if url := _url_identity(event):
            keys.append(("url", url))
        if title and body and body != title:
            keys.append(("text", title, body))
        for key in keys:
            if key in identities:
                parents[root(index)] = root(identities[key])
            identities[key] = index

    groups = {}
    for index, event in enumerate(events):
        group = root(index)
        previous = groups.get(group)
        if previous is None:
            previous = dict(event)
            previous["sources"] = [dict(item) for item in (event.get("sources") or []) if isinstance(item, dict)]
            previous["sources"].append(_origin(event))
            groups[group] = previous
        else:
            previous["sources"].extend(dict(item) for item in (event.get("sources") or []) if isinstance(item, dict))
            previous["sources"].append(_origin(event))
            if len(_text(event.get("content") or event.get("summary"))) > len(_text(previous.get("content") or previous.get("summary"))):
                # Preserve a completed analysis if the richer original is pending.
                analysis = previous.get("analysis") or event.get("analysis")
                origins = previous["sources"]
                previous.update(event)
                previous.update(sources=origins, analysis=analysis)
    selected = list(groups.values())
    for event in selected:
        unique, seen = [], set()
        for origin in event["sources"]:
            marker = (origin.get("source_id"), origin.get("source_name"), origin.get("url"))
            if marker not in seen:
                unique.append(origin)
                seen.add(marker)
        event["sources"] = unique
        names = [_text(item.get("source_name") or item.get("publisher")) for item in unique]
        event["source"] = ", ".join(dict.fromkeys(name for name in names if name)) or "未提供出版者"
    return selected


def _importance(event):
    """Transparent editorial hints, never an estimate of future returns."""
    analysis = event.get("analysis") or {}
    extraction = analysis.get("extraction") or {}
    original = " ".join(_text(event.get(key)) for key in ("title", "summary", "content", "news"))
    text = _normal(original + " " + _text(extraction.get("event_type")))
    macro = bool(re.search(r"\b(?:fed|federal reserve|ecb|inflation|cpi|payrolls|gdp|monetary|treasury|tariffs?)\b|聯儲|联储|美聯儲|美联储|央行|利率|通脹|通胀|非農|非农|關稅|关税|宏觀|宏观", text))
    sector = bool(re.search(r"\b(?:sector|industry|regulation|semiconductor|energy|banking)\b|行業|行业|產業|产业|監管|监管|半導體|半导体|能源|銀行|银行", text))
    breadth = 3 if macro else 2 if sector else 1
    numeric_facts = min(len(re.findall(r"\d+(?:[.,]\d+)?(?:\s*[%％]|\s*(?:billion|million|bps)|億|亿|萬|万)?", original)), 8)
    scope = event.get("content_scope")
    text_quality = 0 if scope in {"title_only", "filing_notice"} else 2 if event.get("content") else 1
    information = numeric_facts + text_quality + min(len(original) // 300, 4)
    return breadth, information


def _select_today(events, day):
    today, unknown, outside = [], 0, 0
    for event in events:
        published = _date_label(_published_at(event))
        if not published:
            unknown += 1
        elif published != day:
            outside += 1
        else:
            today.append(dict(event, published_at=_published_at(event)))
    unique = _deduplicate(today)
    unique.sort(key=_importance, reverse=True)
    return unique, {"hong_kong_date": day, "date_basis": "publication_date",
                    "today_article_count": len(today), "unique_event_count": len(unique),
                    "duplicates_merged": len(today) - len(unique),
                    "unknown_date_excluded": unknown, "outside_date_excluded": outside,
                    "ranking": "market_scope_and_information"}


def generate_daily_report(min_confidence: float = 0.7, *, as_of=None) -> str:
    """Render a deterministic digest of today's completed analyses."""
    day = _day(as_of)
    events, selection = _select_today(load_report_events(), day)
    selected = [event for event in events
                if (event.get("analysis") or {}).get("overall_confidence", 0) >= min_confidence]
    lines = [f"# 新聞事件摘要 {day}（香港日期）\n",
             f"共 {len(selected)} 則今日已分析事件（置信度篩選 ≥ {min_confidence:.0%}）\n",
             "本摘要由已保存的事件分析整理；按市場影響範圍與資訊量排列，未進行跨事件 AI 論說。\n",
             f"已排除日期未知 {selection['unknown_date_excluded']} 則、非當日 {selection['outside_date_excluded']} 則；"
             f"合併重複 {selection['duplicates_merged']} 則。\n"]
    for index, event in enumerate(selected, 1):
        try:
            result = AnalysisResult(**event.get("analysis", {}))
            title = event.get("title") or result.extraction.fact or event.get("source", "未提供出版者")
            lines.append(f"## {index}. {title}")
            lines.append(f"出版者：{event.get('source', '未提供出版者')}；發佈時間：{event.get('published_at')}")
            lines.append(format_result(result))
            if event.get("source_url"):
                lines.append(f"來源：{event['source_url']}（{event.get('content_scope', 'provided_text')}）")
            lines.append("\n---\n")
        except (ValueError, TypeError):
            lines.append(f"## {index}. 已保存的事件分析格式無法辨識\n")
    if not selected:
        lines.append("今日沒有日期可確認、符合篩選的已分析事件。")
    return "\n".join(lines)


def generate_daily_essay(*, api_key, provider="deepseek", model=None, as_of=None,
                         on_progress=None, on_draft=None, register_cancel=None) -> dict:
    """Use original daily articles in the shared plan/write/review workflow."""
    day = _day(as_of)
    events, selection = _select_today(_load_daily_articles(), day)
    articles = []
    manual_ids = set()
    for event in events:
        article = dict(event)
        article.update(id=event.get("id") or "daily:" + hashlib.sha256(
            "\n".join((_url_identity(event) or "", _text(event.get("title")),
                       _text(event.get("content") or event.get("summary")), day)).encode()).hexdigest(),
            source_name=event.get("source"), url=event.get("source_url") or event.get("url"))
        if event.get("original_text_source") == "manual":
            manual_ids.add(article["id"])
            if _text(article.get("content") or article.get("summary")) == _text(article.get("title")):
                article["title"] = "人工提供的新聞文字"
        articles.append(article)
    sources = prepare_sources(articles, max_articles=30)
    for source in sources:
        if source.get("article_id") in manual_ids:
            source["source_type"] = "manual"
            for origin in source["origins"]:
                if origin.get("source_id") in {"", "legacy"}:
                    origin["source_type"] = "manual"
    if not sources:
        raise ResearchError("empty_sources")
    if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ProviderConfigurationError("missing_key")
    selection["included_source_count"] = len(sources)
    selection["source_limit"] = 30

    def draft_ready(draft):
        draft = dict(draft)
        draft["metadata"] = {**draft.get("metadata", {}), "daily_selection": dict(selection)}
        if on_draft:
            on_draft(draft)

    # The shared workflow enriches up to six URLs once, then plans, writes and reviews.
    result = generate_research_workflow(
        DAILY_TOPIC, sources, api_key=api_key, provider=provider, model=model, as_of=day,
        instructions=_DAILY_INSTRUCTIONS, writing_style="argumentative", report_mode="argumentative",
        on_progress=on_progress, on_draft=draft_ready if on_draft else None,
        register_cancel=register_cancel)
    result["metadata"] = {**result.get("metadata", {}), "daily_selection": selection}
    result.setdefault("sources", sources)
    return result


if __name__ == "__main__":
    print(generate_daily_report())
