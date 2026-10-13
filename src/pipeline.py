"""
Investat → 分析器 → 存储 → 日报 管道
"""

import os
import json
from datetime import datetime
from .investat import list_articles, get_article
from .analyzer import analyze_news
from .collector import save_event, load_events, content_hash


def pull_and_analyze(limit: int = 5, model: str = None) -> list[dict]:
    """
    从 Investat 拉最新文章，读全文，用分析器生成结构化结论，并去重存储。
    """
    model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
    articles = list_articles(limit=limit)
    results = []

    for item in articles:
        aid = item["id"]
        title = item.get("title", "")
        summary = item.get("summary", "")
        institution = item.get("institution", "")

        # 先用摘要判断是否已处理
        preview = f"{title}\n{summary}"
        if any(e.get("hash") == content_hash(preview) for e in load_events()):
            print(f"跳过已存在: {title[:40]}")
            continue

        print(f"处理: {title[:50]}...")
        try:
            full = get_article(aid)
            body = full.get("body", summary) or summary
            text = f"【{institution}】{title}\n\n{body}"

            analysis = analyze_news(text, model=model)
            saved = save_event(
                text,
                analysis.model_dump(),
                source=f"investat:{institution}",
            )
            results.append({
                "id": aid,
                "title": title,
                "saved": saved,
                "analysis": analysis.model_dump(),
            })
        except Exception as e:
            print(f"  失败: {e}")
            results.append({"id": aid, "title": title, "error": str(e)})

    return results


if __name__ == "__main__":
    print(f"开始拉取并分析 Investat 文章 {datetime.utcnow().isoformat()}")
    out = pull_and_analyze(limit=3)
    print(json.dumps({"processed": len(out)}, ensure_ascii=False, indent=2))
