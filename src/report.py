"""
日報生成骨架
只展開高置信度事件，按重要程度排序。
"""

from datetime import datetime
from .collector import load_events
from .analyzer import format_result, AnalysisResult


def generate_daily_report(min_confidence: float = 0.7) -> str:
    events = load_events()
    # 過濾高置信度
    high_conf = []
    for e in events:
        conf = e.get("analysis", {}).get("overall_confidence", 0)
        if conf >= min_confidence:
            high_conf.append(e)

    # 簡單排序：置信度降序
    high_conf.sort(key=lambda x: x.get("analysis", {}).get("overall_confidence", 0), reverse=True)

    lines = [f"# 新聞影響日報 {datetime.utcnow().date()}\n"]
    lines.append(f"共 {len(high_conf)} 則高置信度事件（閾值 ≥ {min_confidence:.0%}）\n")

    for i, e in enumerate(high_conf, 1):
        analysis_dict = e.get("analysis", {})
        try:
            # 重建物件以便用 format_result
            result = AnalysisResult(**analysis_dict)
            lines.append(f"## {i}. {e.get('source', 'unknown')} | 置信度 {result.overall_confidence:.0%}")
            lines.append(format_result(result))
            lines.append("\n---\n")
        except Exception:
            lines.append(f"## {i}. 解析失敗\n")

    if not high_conf:
        lines.append("今日無高置信度事件。")

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_daily_report())
