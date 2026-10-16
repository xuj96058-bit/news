"""Conservative, shared metadata normalization for research and public feeds.

Only supplied names and identifiers are normalized. Nothing here infers a stock
exchange from an unqualified number or infers coverage from article prose.
"""

from __future__ import annotations

import re
import unicodedata


INSTITUTION_ALIASES = {
    "Morgan Stanley": ("Morgan Stanley", "摩根士丹利", "大摩"),
    "J.P. Morgan": ("J.P. Morgan", "JP Morgan", "JPMorgan", "JPMorgan Chase", "摩根大通", "小摩"),
    "Goldman Sachs": ("Goldman Sachs", "高盛"),
    "UBS": ("UBS", "瑞銀", "瑞银", "瑞士銀行", "瑞士银行"),
    "BlackRock": ("BlackRock", "BlackRock Investment Institute", "貝萊德", "贝莱德"),
    "Apollo": ("Apollo", "Apollo Global Management", "阿波羅", "阿波罗"),
    "Citi": ("Citi", "Citigroup", "花旗"),
    "Bank of America": ("Bank of America", "BofA", "美國銀行", "美国银行", "美銀", "美银"),
    "HSBC": ("HSBC", "匯豐", "汇丰"),
    "Deutsche Bank": ("Deutsche Bank", "德意志銀行", "德意志银行", "德銀", "德银"),
}
METADATA_FIELDS = (
    "institution", "institution_original", "author", "publisher", "provider",
    "tickers", "asset_classes", "topics",
)
_SECRET = re.compile(r"(?i)(?:\b(?:sk-|ift__)[A-Za-z0-9_-]{8,}|\bbearer\s+\S+|"
                     r"\b(?:api[_-]?key|authorization|(?:access[_-]?|refresh[_-]?|mcp[_-]?)?token|password)\s*[:=]\s*\S+)")


def clean_text(value) -> str:
    """Accept plain supplied labels, never arbitrary nested provider objects."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("displayName") or value.get("label") or ""
    if not isinstance(value, str):
        return ""
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    return "" if _SECRET.search(value) else value


def _key(value) -> str:
    return re.sub(r"[\s.]+", "", clean_text(value)).casefold()


_INSTITUTION_LOOKUP = {_key(alias): name for name, aliases in INSTITUTION_ALIASES.items() for alias in aliases}


def canonical_institution(value) -> str:
    name = clean_text(value)
    return _INSTITUTION_LOOKUP.get(_key(name), name)


def institution_aliases(value) -> tuple[str, ...]:
    name = canonical_institution(value)
    return INSTITUTION_ALIASES.get(name, (name,))


def normalize_ticker(value) -> str:
    """Standardize explicit HK/Shanghai/Shenzhen/Beijing qualifiers only.

    Examples: HKEX:700 -> 0700.HK, 600519.SS -> 600519.SH,
    SZSE:000001 -> 000001.SZ. A bare 700 or 600519 remains unqualified.
    US share-class symbols such as BRK.B retain their punctuation.
    """
    if isinstance(value, dict):
        symbol = value.get("symbol") or value.get("ticker") or value.get("code")
        exchange = clean_text(value.get("exchange") or value.get("market"))
        value = f"{exchange}:{symbol}" if exchange and symbol else symbol
    symbol = clean_text(value).upper().lstrip("$")
    if not symbol or not re.fullmatch(r"[A-Z0-9.^:_-]{1,40}", symbol):
        return ""
    exchanges = {"HK": "HK", "HKEX": "HK", "HKG": "HK", "SEHK": "HK",
                 "SH": "SH", "SS": "SH", "SSE": "SH", "SHSE": "SH",
                 "SZ": "SZ", "SZSE": "SZ", "BJ": "BJ", "BSE": "BJ"}
    match = re.fullmatch(r"([A-Z]+)[:.]([0-9]+)", symbol)
    if match and match[1] in exchanges:
        market, code = exchanges[match[1]], match[2]
    else:
        match = re.fullmatch(r"([0-9]+)[.:]([A-Z]+)", symbol)
        if not match or match[2] not in exchanges:
            return symbol
        code, market = match[1], exchanges[match[2]]
    if market == "HK" and 1 <= len(code) <= 5:
        # HKEX exports often pad to five places; market data feeds use four
        # for ordinary equities. Both spellings identify the same stock.
        return f"{str(int(code)).zfill(4)}.HK"
    if market in {"SH", "SZ", "BJ"} and len(code) == 6:
        return f"{code}.{market}"
    return symbol


def labels(value, *, ticker=False) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;，；]", value)
    elif isinstance(value, dict):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        value = []
    result = []
    for item in value:
        label = normalize_ticker(item) if ticker else clean_text(item)
        if label and label.casefold() not in {existing.casefold() for existing in result}:
            result.append(label)
    return result


def normalize_metadata(record: dict) -> dict:
    institution = (record.get("institution") or record.get("institutionName")
                   or record.get("institution_name"))
    # Older Investat rows only recorded attribution in their display label.
    if not institution and record.get("source_id") == "investat":
        source_name = clean_text(record.get("source_name"))
        institution = source_name.partition(" / ")[2]
    authors = labels(record.get("author") or record.get("authors") or record.get("authorName"))
    return {
        "institution": canonical_institution(institution),
        "institution_original": clean_text(record.get("institution_original") or institution),
        "author": "; ".join(authors),
        "publisher": clean_text(record.get("publisher") or record.get("publisherName")),
        "provider": clean_text(record.get("provider")) or ("Investat" if record.get("source_id") == "investat" else ""),
        "tickers": labels(record.get("tickers") or record.get("ticker") or record.get("symbols"), ticker=True),
        "asset_classes": labels(record.get("asset_classes") or record.get("assetClasses") or record.get("assetClass")),
        "topics": labels(record.get("topics") or record.get("themes") or record.get("tags")),
    }


def safe_saved_text(value, *, max_length=1000) -> str:
    if not isinstance(value, str):
        raise ValueError("Search values must be text")
    if _SECRET.search(unicodedata.normalize("NFKC", value)):
        raise ValueError("Credentials cannot be saved in a search")
    if len(value) > max_length:
        raise ValueError("Search value is too long")
    return value.strip()
