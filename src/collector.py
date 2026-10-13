"""
新聞自動收集與去重骨架
目前為 placeholder，可接 RSS / NewsAPI / 手動輸入。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.jsonl"


def content_hash(text: str) -> str:
    return hashlib.md5(text.strip().encode()).hexdigest()


def load_events() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    events = []
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events


def is_duplicate(text: str, existing: Optional[list[dict]] = None) -> bool:
    h = content_hash(text)
    events = existing or load_events()
    return any(e.get("hash") == h for e in events)


def save_event(news_text: str, analysis: dict, source: str = "manual"):
    """儲存一則分析過的新聞（已去重）。"""
    if is_duplicate(news_text):
        return False
    record = {
        "hash": content_hash(news_text),
        "timestamp": datetime.utcnow().isoformat(),
        "source": source,
        "news": news_text[:500],  # 截斷以省空間
        "analysis": analysis,
    }
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def collect_sample():
    """示例收集（可替換為真實 RSS / API）。"""
    samples = [
        "英伟达与思科推出 USFP 光模组，单模组带宽达 12.8Tb/s，用于 AI 工厂互连。",
        "美聯儲維持利率不變，但表示將根據數據調整政策路徑。",
    ]
    return samples
