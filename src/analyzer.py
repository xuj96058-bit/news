"""
金融新聞分析器
目標：給出分析師水平的可解釋結論，而不是黑箱分數。
"""

import os
import json
import re
from typing import Any
from openai import OpenAI
from pydantic import BaseModel, Field


class EventExtraction(BaseModel):
    event_type: str = Field(description="事件類型，例如：財報、產品發布、監管、併購、宏觀數據、法律訴訟 等")
    entities: list[str] = Field(description="新聞中提及的公司、機構或關鍵實體")
    fact: str = Field(description="核心事實的一句話摘要")
    direction: str = Field(description="positive / negative / neutral / mixed")
    confidence: float = Field(ge=0.0, le=1.0, description="抽取置信度")
    reasoning: str = Field(description="為什麼這樣判斷方向與置信度")


class ImpactItem(BaseModel):
    ticker: str = Field(description="標的代碼，例如 NVDA, QQQ, TLT")
    direction: str = Field(description="positive / negative / neutral")
    strength: str = Field(description="high / medium / low")
    path: str = Field(description="傳導路徑：直接 / 供應鏈 / 板塊 / 宏觀")
    reason: str = Field(description="一句話說明影響邏輯")


class AnalysisResult(BaseModel):
    extraction: EventExtraction
    impacts: list[ImpactItem]
    summary: str = Field(description="整體結論，分析師口吻")
    overall_confidence: float = Field(ge=0.0, le=1.0)


# 擴展的實體 → 標的映射（中英都覆蓋，可持續擴展）
ENTITY_TO_TICKER = {
    "NVIDIA": "NVDA", "英伟达": "NVDA", "英偉達": "NVDA", "英偉达": "NVDA",
    "Apple": "AAPL", "蘋果": "AAPL", "苹果": "AAPL",
    "Microsoft": "MSFT", "微软": "MSFT",
    "Amazon": "AMZN", "亞馬遜": "AMZN", "亚马逊": "AMZN",
    "Tesla": "TSLA", "特斯拉": "TSLA",
    "Meta": "META", "徵象": "META", "Facebook": "META",
    "Google": "GOOGL", "Alphabet": "GOOGL", "谷歌": "GOOGL",
    "Cisco": "CSCO", "思科": "CSCO",
    "TSMC": "TSM", "台積電": "TSM", "台积电": "TSM",
    "Broadcom": "AVGO", "博通": "AVGO",
    "AMD": "AMD", "超微": "AMD",
    "Intel": "INTC", "英特爾": "INTC",
    "QQQ": "QQQ", "Nasdaq": "QQQ", "納指": "QQQ", "纳指": "QQQ",
    "SPY": "SPY", "S&P 500": "SPY", "標普": "SPY",
    "Fed": "TLT", "聯邦儲備": "TLT", "美聯儲": "TLT", "美联储": "TLT",
    "Treasury": "TLT", "美債": "TLT",
    "Berkshire": "BRK.B", "伯克希爾": "BRK.B",
    "JPMorgan": "JPM", "摩根大通": "JPM",
    "Goldman": "GS", "高盛": "GS",
}

KNOWN_TICKERS = {
    "NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "META", "GOOGL", "GOOG",
    "CSCO", "TSM", "AVGO", "AMD", "INTC", "QQQ", "SPY", "TLT",
    "BRK.B", "JPM", "GS", "XLF", "XLK", "SMH", "MRVL", "VRT", "CEG", "ETN", "NEE",
}


def normalize_entity(name: str) -> str:
    return re.sub(r"\s+", "", name).strip()


def map_entities_to_tickers(entities: list[str]) -> dict[str, str]:
    mapping = {}
    for ent in entities:
        norm = normalize_entity(ent)
        if norm in ENTITY_TO_TICKER:
            mapping[norm] = ENTITY_TO_TICKER[norm]
            continue
        for key, ticker in ENTITY_TO_TICKER.items():
            if key.lower() == norm.lower() or key.lower() in norm.lower() or norm.lower() in key.lower():
                mapping[norm] = ticker
                break
    return mapping


def post_process_impacts(result: AnalysisResult) -> AnalysisResult:
    entity_map = map_entities_to_tickers(result.extraction.entities)
    trusted_tickers = set(entity_map.values())
    validated_impacts = []
    seen = set()

    for ticker in trusted_tickers:
        if ticker not in seen:
            existing = next((imp for imp in result.impacts if imp.ticker.upper() == ticker), None)
            if existing:
                validated_impacts.append(existing)
            else:
                validated_impacts.append(ImpactItem(
                    ticker=ticker,
                    direction=result.extraction.direction if result.extraction.direction in {"positive", "negative", "neutral"} else "neutral",
                    strength="medium",
                    path="直接",
                    reason=f"來自實體映射：{', '.join(k for k, v in entity_map.items() if v == ticker)}",
                ))
            seen.add(ticker)

    for imp in result.impacts:
        t = imp.ticker.upper().replace(".", "")
        if t in KNOWN_TICKERS or imp.ticker.upper() in trusted_tickers:
            if imp.ticker.upper() not in seen:
                validated_impacts.append(imp)
                seen.add(imp.ticker.upper())

    result.impacts = validated_impacts
    return result


def get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    if not api_key:
        raise RuntimeError("請設定環境變數 OPENAI_API_KEY")
    return OpenAI(api_key=api_key, base_url=base_url)


def analyze_news(news_text: str, model: str = None) -> AnalysisResult:
    """
    對一則新聞做分析師水平的結構化分析。
    """
    model = model or os.getenv("LLM_MODEL", "deepseek-v4-pro")
    client = get_client()

    system = """你是一位經驗豐富的金融分析師。
請對給定新聞做深度分析，輸出可解釋的結構化結論。
必須只輸出純 JSON，不要有任何額外文字。

JSON Schema:
{
  "extraction": {
    "event_type": "...",
    "entities": ["..."],
    "fact": "...",
    "direction": "positive|negative|neutral|mixed",
    "confidence": 0.85,
    "reasoning": "..."
  },
  "impacts": [
    {
      "ticker": "NVDA",
      "direction": "positive",
      "strength": "high",
      "path": "直接",
      "reason": "..."
    }
  ],
  "summary": "分析師口吻的整體結論（1-3 句）",
  "overall_confidence": 0.8
}

要求：
- 支援中文與英文新聞
- 標的請優先使用美股常見代碼（NVDA, AAPL, MSFT, QQQ, TLT 等）
- 傳導路徑要具體：直接影響公司本身 / 供應鏈 / 板塊輪動 / 宏觀
- 每個結論都要有清楚理由，不能只給分數
- 置信度要實事求是，不確定的事件要調低
- entities 請尽量保留原始名稱（中文或英文）
"""

    user = f"新聞內容：\n{news_text.strip()}"

    # DeepSeek V4 Pro 默認會走 thinking，這裡關掉以保證 JSON 輸出穩定
    extra = {}
    if "deepseek" in model.lower():
        extra["thinking"] = {"type": "disabled"}

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=2000,
        response_format={"type": "json_object"},
        extra_body=extra if extra else None,
    )

    raw = resp.choices[0].message.content
    data = json.loads(raw)
    result = AnalysisResult(**data)
    return post_process_impacts(result)


def format_result(result: AnalysisResult) -> str:
    lines = []
    e = result.extraction
    lines.append(f"【事件類型】{e.event_type}")
    lines.append(f"【核心事實】{e.fact}")
    lines.append(f"【方向】{e.direction}  【置信度】{e.confidence:.0%}")
    lines.append(f"【抽取理由】{e.reasoning}")
    lines.append("")
    lines.append("【受影響標的】")
    if not result.impacts:
        lines.append("  （無明確標的）")
    for imp in result.impacts:
        lines.append(f"  • {imp.ticker}  {imp.direction} ({imp.strength})  [{imp.path}]")
        lines.append(f"    {imp.reason}")
    lines.append("")
    lines.append(f"【整體結論】{result.summary}")
    lines.append(f"【整體置信度】{result.overall_confidence:.0%}")
    return "\n".join(lines)
