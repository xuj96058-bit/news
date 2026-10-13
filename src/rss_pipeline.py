"""Collect a local financial-news inbox; AI analysis is an explicit opt-in."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .feeds import DEFAULT_SOURCES, FeedFetchError, fetch_source, load_sources
from .news_store import DEFAULT_DB_PATH, NewsStore
from .sec_universe import (DEFAULT_UNIVERSE, SecConfigError, collect_sec_filings,
                           load_universe, refresh_universe)


def _utc_date(value):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def collect_sources(sources, store, *, limit_per_source=50, max_age_days=30,
                    timeout=15, workers=4, fetcher=None, now=None):
    """Fetch independently, then ingest on the calling thread.

    One failed source does not prevent the other feeds from being saved. Feed
    summaries remain summaries; the collector never fetches article pages.
    """
    if limit_per_source < 1 or max_age_days < 0 or workers < 1 or timeout <= 0:
        raise ValueError("Invalid collection limits")
    fetcher = fetcher or fetch_source
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days) if max_age_days else None
    enabled = [source for source in sources if source.enabled]
    report = {"fetched_at": now.isoformat(), "sources": [], "added": 0,
              "updated": 0, "duplicates": 0, "failed_sources": 0}
    with ThreadPoolExecutor(max_workers=min(workers, 8)) as executor:
        jobs = [(source, executor.submit(fetcher, source, timeout=timeout)) for source in enabled]
        for source, job in jobs:
            outcome = {"id": source.id, "name": source.name, "kind": source.kind}
            try:
                items = [entry.to_dict() if hasattr(entry, "to_dict") else dict(entry)
                         for entry in job.result()]
                # Unknown dates are retained and labelled, not invented.
                recent = [item for item in items if cutoff is None
                          or _utc_date(item.get("published_at")) is None
                          or _utc_date(item["published_at"]) >= cutoff]
                recent.sort(key=lambda item: _utc_date(item.get("published_at"))
                            or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                selected = recent[:limit_per_source]
                counts = store.ingest(selected)
                outcome.update(status="ok" if items else "empty", fetched=len(items),
                               selected=len(selected), older_skipped=len(items) - len(recent),
                               limited=len(recent) - len(selected), **counts)
                for key in ("added", "updated", "duplicates"):
                    report[key] += counts[key]
            except Exception as exc:
                # Only our fetcher's deliberately safe errors are shown verbatim.
                message = str(exc) if isinstance(exc, FeedFetchError) else type(exc).__name__
                outcome.update(status="error", error=message)
                report["failed_sources"] += 1
            report["sources"].append(outcome)
    report["database"] = store.stats()
    return report


def collect_sec(store, *, universe_path=DEFAULT_UNIVERSE, max_pages=10, timeout=15):
    """Collect filing notices for the union of the two index watchlists."""
    try:
        universe = load_universe(universe_path)
        articles, diagnostics = collect_sec_filings(universe, max_pages=max_pages, timeout=timeout)
        counts = store.ingest(article.to_dict() for article in articles)
        return {**counts, "universe": universe["counts"],
                "membership_fetched_at": universe["fetched_at"],
                "membership_sources": universe["sources"], "sources": diagnostics,
                "notice_only": True,
                "coverage": "SEC current-feed window only; not a historical backfill"}
    except (SecConfigError, FeedFetchError) as exc:
        return {"added": 0, "updated": 0, "duplicates": 0,
                "sources": [{"source_id": "sec", "status": "error", "error": str(exc)}]}


def analyze_pending(store, *, limit=10, analyzer=None, universe_path=None):
    """Analyze unchanged inbox items once, retaining safe errors for retries."""
    if limit < 1:
        raise ValueError("Analysis limit must be positive")
    if analyzer is None:
        from .analyzer import analyze_news
        analyzer = lambda text: analyze_news(text, universe_path=universe_path)
    results = {"analyzed": 0, "failed": 0, "insufficient_content": 0}
    for article in store.pending(limit=limit, require_text=True):
        body = article.get("content") or article.get("summary") or ""
        if not body.strip() or body.strip() == article["title"].strip():
            results["insufficient_content"] += 1
            continue
        source_names = ", ".join(dict.fromkeys(
            f"{s['source_name']} ({s['source_kind']})" for s in article["sources"]))
        text = (f"來源：{source_names}\n來源類型：{article['source_kind']}\n"
                f"發布時間：{article.get('published_at') or '未提供'}\n"
                f"原文連結：{article['url']}\n"
                f"資料範圍：{article['content_scope']}（僅來源 feed 提供的內容，可能不是全文）\n"
                f"標題：{article['title']}\n\n{body}")
        try:
            result = analyzer(text)
            analysis = result.model_dump() if hasattr(result, "model_dump") else result
            store.mark_analyzed(article["id"], analysis,
                                expected_content_hash=article["content_hash"])
            results["analyzed"] += 1
        except Exception as exc:
            store.mark_error(article["id"], type(exc).__name__)
            results["failed"] += 1
    return results


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def export_jsonl(store, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for article in store.export_articles():
            output.write(json.dumps(article, ensure_ascii=False) + "\n")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="抓取財經 RSS／Atom 與金管局新聞；預設不呼叫 AI")
    parser.add_argument("--config", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--source", action="append", help="只抓指定來源 ID，可重複使用")
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument("--limit-per-source", type=int, default=50)
    parser.add_argument("--max-age-days", type=int, default=30, help="只收最近幾天；0 表示不篩日期")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sec", action="store_true", help="另抓標普 500＋納斯達克 100 的 SEC 公告")
    parser.add_argument("--sec-only", action="store_true", help="只抓 SEC 公告")
    parser.add_argument("--sec-max-pages", type=int, default=10, help="每種申報最多頁數，1–100")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--refresh-sec-universe", action="store_true", help="刷新指數名單及官方 SEC CIK 對照後退出")
    parser.add_argument("--analyze", action="store_true", help="另外分析待處理新聞，會使用 LLM API")
    parser.add_argument("--analysis-only", action="store_true", help="只處理本機分析佇列，不重新抓新聞")
    parser.add_argument("--analysis-limit", type=int, default=10)
    parser.add_argument("--export", type=Path, help="將新聞庫匯出為 UTF-8 JSONL")
    args = parser.parse_args(argv)
    if (args.limit_per_source < 1 or args.max_age_days < 0 or args.timeout <= 0
            or args.workers < 1 or args.analysis_limit < 1 or not 1 <= args.sec_max_pages <= 100):
        parser.error("數量及逾時需為正數，max-age-days 不可為負數")
    if args.analysis_only and (args.sec or args.sec_only):
        parser.error("analysis-only 不能同時抓取 SEC")
    if args.refresh_sec_universe:
        try:
            universe = refresh_universe(args.universe, timeout=args.timeout)
        except (SecConfigError, FeedFetchError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"status": "ok", "counts": universe["counts"],
                          "sources": universe["sources"]}, ensure_ascii=False, indent=2))
        return 0
    try:
        sources = load_sources(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    if args.source:
        unknown = set(args.source) - {source.id for source in sources}
        if unknown:
            parser.error("未知來源 ID：" + ", ".join(sorted(unknown)))
        sources = [source for source in sources if source.id in args.source]
    if args.list_sources:
        print(json.dumps([{"id": s.id, "name": s.name, "kind": s.kind,
                           "format": s.format, "enabled": s.enabled} for s in sources],
                         ensure_ascii=False, indent=2))
        return 0
    if not args.analysis_only and not args.sec_only and not any(source.enabled for source in sources):
        parser.error("沒有啟用的來源")
    with NewsStore(args.db) as store:
        report = {"sources": [], "failed_sources": 0, "added": 0, "updated": 0, "duplicates": 0}
        if not args.analysis_only and not args.sec_only:
            report = collect_sources(sources, store, limit_per_source=args.limit_per_source,
                                     max_age_days=args.max_age_days, timeout=args.timeout,
                                     workers=args.workers)
        if args.sec or args.sec_only:
            report["sec"] = collect_sec(store, universe_path=args.universe,
                                         max_pages=args.sec_max_pages, timeout=args.timeout)
            for key in ("added", "updated", "duplicates"):
                report[key] += report["sec"][key]
            for item in report["sec"]["sources"]:
                report["sources"].append({**item, "id": item["source_id"],
                                          "name": "SEC EDGAR", "kind": "official"})
                if item["status"] in {"error", "partial"}:
                    report["failed_sources"] += 1
        if args.analyze or args.analysis_only:
            report["analysis"] = analyze_pending(store, limit=args.analysis_limit, universe_path=args.universe)
        report["database"] = store.stats()
        if not args.analysis_only:
            write_json(args.db.parent / ("sec_status.json" if args.sec_only else "feed_status.json"), report)
        if args.export:
            export_jsonl(store, args.export)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("analysis", {}).get("failed"):
        return 2
    failures = report["failed_sources"]
    if not failures:
        return 0
    all_errors = bool(report["sources"]) and all(s["status"] == "error" for s in report["sources"])
    saved_or_seen = report["added"] + report["updated"] + report["duplicates"]
    return 1 if all_errors and not saved_or_seen else 2


if __name__ == "__main__":
    raise SystemExit(main())
