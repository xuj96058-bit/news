"""Persistent charts with source provenance, explicit verification and report evidence."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
import uuid

DEFAULT_CHART_DB = Path(__file__).resolve().parents[1] / "data" / "chartbook.sqlite3"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_POINTS = 5000
_TEXT_FIELDS = ("title", "publisher", "source_url", "publication_date", "metric", "unit",
                "observation_start", "observation_end", "notes", "conclusion", "source_article_id",
                "source_content_hash", "source_excerpt", "method", "verification")


def _day(value, required=True):
    if not value and not required:
        return ""
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        raise ValueError("日期需要 YYYY-MM-DD 格式。") from None


def _points(rows):
    if not isinstance(rows, list) or len(rows) > MAX_POINTS:
        raise ValueError("數據最多5000筆。")
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or isinstance(row.get("value"), bool):
            raise ValueError("每筆數據需要日期和數值。")
        day = _day(row.get("date"))
        try:
            value = float(row["value"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ValueError("數值必須是有限的數字，不能含百分號或千分位。") from None
        if not math.isfinite(value):
            raise ValueError("數值必須是有限的數字。")
        try:
            exact_value = Decimal(str(row["value"]).strip())
        except InvalidOperation:
            raise ValueError("數值格式錯誤。") from None
        if Decimal(str(value)) != exact_value:
            # Charts use doubles; reject unsupported precision rather than
            # silently changing source data (including underflow to zero).
            raise ValueError("數值精度超出圖表支援範圍，請先明確整理數值精度再匯入。")
        series = str(row.get("series") or "數值").strip()
        if len(series) > 200:
            raise ValueError("序列名稱不能超過200字。")
        if (series, day) in seen:
            raise ValueError("同一序列、同一日期不能重複。")
        seen.add((series, day))
        output.append({"date": day, "value": value, "series": series})
    return sorted(output, key=lambda row: (row["date"], row["series"]))


def parse_chart_csv(text):
    if not isinstance(text, str) or len(text) > 2_000_000:
        raise ValueError("CSV超過大小限制。")
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
        if (not reader.fieldnames or not {"date", "value"} <= set(reader.fieldnames)
                or len(reader.fieldnames) != len(set(reader.fieldnames))
                or set(reader.fieldnames) - {"date", "value", "series"}):
            raise ValueError("CSV需要唯一的date,value欄位；可另加series欄位。")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("CSV每列的欄位數必須與標題一致；數值不能含未加引號的千分位。")
            if len(rows) == MAX_POINTS:
                raise ValueError("數據最多5000筆。")
            rows.append(row)
    except csv.Error:
        raise ValueError("CSV引號或欄位格式錯誤。") from None
    if not rows:
        raise ValueError("CSV沒有數據。")
    return _points(rows)


def _image(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("圖片大小需在8 MiB以內。")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 30_000_000
                    or getattr(image, "n_frames", 1) != 1):
                raise ValueError()
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()  # Some formats do not decode pixel data during verify().
    except Exception:
        raise ValueError("只接受有效的單張PNG、JPEG或WebP圖片（最多3000萬像素）。") from None
    return data


def _validated(payload, image_bytes=None):
    if not isinstance(payload, dict):
        raise ValueError("圖表資料格式錯誤。")
    output = {key: str(payload.get(key) or "").strip() for key in _TEXT_FIELDS}
    for field in ("title", "publisher", "metric", "unit"):
        if not output[field] or len(output[field]) > 300:
            raise ValueError("請填寫標題、發布機構、指標及單位（各不超過300字）。")
    if any(len(value) > 12000 for value in output.values()):
        raise ValueError("圖表說明過長。")
    # Use the same provenance policy as report snapshots: strip credential
    # query parameters and fragments before persistence, hashing or export.
    from .research import _url
    source_url = _url(output["source_url"])
    if source_url is None:
        raise ValueError("請填寫不含帳號密碼的原始HTTP(S)來源連結。")
    output["source_url"] = source_url
    output["publication_date"] = _day(output["publication_date"], required=False)
    output["method"] = output["method"] or "manual"
    output["verification"] = output["verification"] or "unverified"
    if output["method"] not in {"manual", "ocr", "source_data"} or output["verification"] not in {"unverified", "human_verified"}:
        raise ValueError("資料取得方式或核對狀態無效。")
    output["points"] = _points(payload.get("points") or [])
    if output["points"]:
        start, end = output["points"][0]["date"], output["points"][-1]["date"]
        if output["observation_start"] and _day(output["observation_start"]) != start:
            raise ValueError("觀測起日與數據不一致。")
        if output["observation_end"] and _day(output["observation_end"]) != end:
            raise ValueError("觀測迄日與數據不一致。")
        output.update(observation_start=start, observation_end=end, kind="data")
    elif image_bytes is not None:
        output.update(observation_start=_day(output["observation_start"]),
                      observation_end=_day(output["observation_end"]), kind="image")
        if output["observation_start"] > output["observation_end"]:
            raise ValueError("觀測起日不能晚於迄日。")
    else:
        raise ValueError("請上傳圖表圖片或提供數據。")
    if image_bytes is not None:
        _image(image_bytes)
    output["image_sha256"] = hashlib.sha256(image_bytes).hexdigest() if image_bytes else ""
    output["content_hash"] = hashlib.sha256(json.dumps(output, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return output


class ChartbookStore:
    def __init__(self, path=DEFAULT_CHART_DB):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""CREATE TABLE IF NOT EXISTS charts (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, image BLOB,
            created_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0)""")
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    def create_chart(self, payload, image_bytes=None):
        chart = _validated(payload, image_bytes)
        identifier, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute("INSERT INTO charts(id,payload,image,created_at) VALUES(?,?,?,?)",
                                    (identifier, json.dumps(chart, ensure_ascii=False), image_bytes, now))
        return self.get_chart(identifier)

    @staticmethod
    def _record(row, include_image=False):
        output = {**json.loads(row["payload"]), "id": row["id"], "created_at": row["created_at"],
                  "archived": bool(row["archived"])}
        if include_image:
            output["image_bytes"] = bytes(row["image"]) if row["image"] else None
        return output

    def get_chart(self, identifier, include_image=False):
        fields = "*" if include_image else "id,payload,created_at,archived"
        row = self.connection.execute(f"SELECT {fields} FROM charts WHERE id=?", (identifier,)).fetchone()
        return self._record(row, include_image) if row else None

    def list_charts(self, query="", limit=25, offset=0):
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise ValueError("分頁範圍無效。")
        # instr treats user input literally, including SQL wildcard characters.
        sql = " FROM charts WHERE archived=0 AND instr(lower(payload),lower(?))>0"
        count = self.connection.execute("SELECT COUNT(*)" + sql, (str(query).strip(),)).fetchone()[0]
        rows = self.connection.execute("SELECT id,payload,created_at,archived" + sql +
                                       " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                                       (str(query).strip(), limit, offset))
        return {"items": [self._record(row) for row in rows], "total": count}

    def archive_chart(self, identifier):
        with self.connection:
            self.connection.execute("UPDATE charts SET archived=1 WHERE id=?", (identifier,))

    def export_json(self, identifier):
        chart = self.get_chart(identifier)
        if chart is None:
            raise KeyError(identifier)
        return json.dumps(chart, ensure_ascii=False, indent=2)


def chart_evidence(chart):
    """Create a bounded textual evidence snapshot; an image is not numeric evidence."""
    status = "使用者標記已人工核對" if chart.get("verification") == "human_verified" else "尚未人工核對"
    lines = [f"圖表：{chart['title']}", f"指標：{chart['metric']}；單位：{chart['unit']}",
             f"觀測期：{chart['observation_start']}至{chart['observation_end']}",
             f"取得方式：{chart['method']}；狀態：{status}",
             f"來源：{chart['source_url']}", f"圖表識別碼：{chart['id']}",
             f"圖表快照 SHA-256：{chart['content_hash']}"]
    if chart.get("source_article_id"):
        lines.append(f"關聯文章識別碼：{chart['source_article_id']}")
        lines.append(f"關聯文章快照 SHA-256：{chart.get('source_content_hash') or '未提供'}")
    points = chart.get("points") or []
    if points:
        lines.append("日期 | 序列 | 數值 | 單位")
        for point in points[:200]:
            # ':g' defaults to six significant digits and silently changes
            # financial observations. Expand the stored float without rounding
            # or exponents so the report's numeric checker can trace it exactly.
            value = format(Decimal(str(point["value"])), "f")
            value = value.rstrip("0").rstrip(".") if "." in value else value
            series = " ".join(str(point["series"]).split()).replace("|", "\\|")
            unit = " ".join(str(chart["unit"]).split()).replace("|", "\\|")
            lines.append(f"{point['date']} | {series} | {value} | {unit}")
        if len(points) > 200:
            lines.append(f"[僅附首200筆，共{len(points)}筆，完整數據見圖表庫]")
    else:
        lines.append("只有來源圖片，未提取數值；圖片說明不能當作已驗證的數據。")
    for field, label in (("source_excerpt", "使用者提供的來源摘錄"), ("notes", "說明"), ("conclusion", "使用者研究判斷")):
        if chart.get(field):
            lines.append(f"{label}：{chart[field]}")
    return {"title": chart["title"], "publisher": chart["publisher"], "url": chart["source_url"],
            "publication_date": chart.get("publication_date") or None,
            "observation_date": chart["observation_start"] + " / " + chart["observation_end"],
            "retrieved_at": chart["created_at"], "source_type": "manual", "content_scope": "chart_evidence",
            "content": "\n".join(lines), "chart_id": chart["id"], "chart_content_hash": chart["content_hash"],
            "source_article_id": chart.get("source_article_id") or "",
            "source_content_hash": chart.get("source_content_hash") or "",
            "image_sha256": chart.get("image_sha256") or ""}
