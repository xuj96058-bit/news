"""Shared display labels for review progress, without changing research facts."""
from __future__ import annotations

from collections.abc import Mapping


def review_completed(metadata):
    if not isinstance(metadata, Mapping):
        return False
    status = metadata.get("review_status")
    stage = metadata.get("workflow_stage") if isinstance(metadata.get("workflow_stage"), str) else None
    if metadata.get("review_error") or not isinstance(status, str) or status not in {"reviewed", "needs_review"}:
        return False
    if stage in {
            "writer_saved", "review_saved", "review_failed", "research_prepared", "preparation_failed"}:
        return False
    return (stage == "review_complete"
            or metadata.get("quality_status") == "reviewed_snapshot")


def _has_errors(metadata):
    return any(isinstance(item, Mapping) and item.get("severity") == "error"
        for key in ("review_findings", "validation_warnings")
        for item in (metadata.get(key) if isinstance(metadata.get(key), list) else []))


def review_status_label(metadata, *, version=None):
    """Describe automation separately from evidence limits and fact verification."""
    if not isinstance(metadata, Mapping):
        return ""
    status = metadata.get("review_status")
    if not isinstance(status, str) or not status:
        return ""
    stage = metadata.get("workflow_stage") if isinstance(metadata.get("workflow_stage"), str) else None
    reviewed = metadata.get("reviewed_version")
    if (isinstance(reviewed, int) and not isinstance(reviewed, bool) and reviewed > 0
            and version is not None and reviewed != version):
        return f"人工修改後待核對（AI審稿基於v{reviewed}）"
    if metadata.get("report_has_body") is False or stage in {"research_prepared", "preparation_failed"}:
        return "研究準備紀錄，正文尚未生成"
    if metadata.get("review_error") or status == "review_failed" or stage == "review_failed":
        return "自動審稿未完成，已保留最後成功保存的正文"
    if review_completed(metadata):
        quality = metadata.get("quality")
        verdict = quality.get("verdict") if isinstance(quality, Mapping) else None
        if verdict == "revise":
            detail = "審稿結果仍要求修訂"
        elif verdict == "insufficient":
            detail = "可支持的論證仍不足"
        elif _has_errors(metadata):
            detail = "程序核驗仍有未解問題"
        elif verdict == "pass":
            audit = metadata.get("claim_audit")
            audit_limited = isinstance(audit, Mapping) and (audit.get("complete") is False or bool(audit.get("unresolved_count")))
            limited = (status == "needs_review" or metadata.get("answer_status") in ("limited", "unanswered")
                or bool(metadata.get("preparation_warnings")) or bool(metadata.get("validation_warnings")) or audit_limited)
            detail = "來源與主張核驗仍有限制" if limited else "模型評審通過"
        else:
            detail = "仍有待核對項目" if status == "needs_review" else "審稿紀錄已保存"
        return "自動審稿已完成；" + detail + "，尚未經獨立事實核實"
    if stage == "review_saved":
        return "已保存階段審稿結果，整輪自動審稿尚未完成"
    if status in {"pending", "draft"} or stage == "writer_saved":
        return "初稿已保存，尚待獨立審稿"
    return "審稿完成狀態仍待核對，尚未經獨立事實核實"
