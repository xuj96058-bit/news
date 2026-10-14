"""Local research planning and evidence-bound arithmetic; no network or AI.

Document names and keywords only identify candidates. A fulfilled requirement
needs a structured number anchored to a source excerpt, including its unit,
period and population. These checks establish traceability, not truthfulness.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
import unicodedata

MAX_ITEMS = 500
MAX_CONTENT = 18000
MAX_OBSERVATIONS = 200
MAX_EXCERPT = 2400
_HAN = r"\u3400-\u9fff"
_TRADITIONAL = str.maketrans({
    "擁": "拥", "擠": "挤", "槓": "杠", "桿": "杆", "業": "业", "倉": "仓", "籌": "筹",
    "資": "资", "產": "产", "債": "债", "餘": "余", "額": "额", "證": "证", "據": "据",
    "數": "数", "較": "较", "歷": "历", "風": "风", "險": "险", "場": "场", "國": "国",
    "聯": "联", "儲": "储", "脹": "胀", "貸": "贷", "漲": "涨", "價": "价", "權": "权",
    "淨": "净", "頭": "头", "買": "买", "賣": "卖", "術": "术", "導": "导", "體": "体",
    "獲": "获", "營": "营", "賺": "赚", "幣": "币", "總": "总", "與": "与", "為": "为",
    "萬": "万", "億": "亿", "類": "类", "機": "机", "構": "构", "報": "报", "臺": "台",
    "變": "变", "動": "动", "時": "时", "間": "间", "長": "长", "線": "线", "戶": "户",
    "無": "无", "缺": "缺", "並": "并", "應": "应",
})
_CONCEPTS = {
    "crowding": ("crowding", "crowded", "拥挤", "拥挤交易"),
    "leverage": ("leverage", "leveraged", "杠杆"),
    "sector": ("sector", "sectors", "行业", "板块"),
    "positioning": ("positioning", "holdings", "持仓", "仓位", "配置"),
    "margin": ("margin debt", "margin balances", "debit balances", "保证金", "融资融券"),
    "borrowing": ("borrowing", "borrowings", "prime broker", "借款", "融资"),
    "concentration": ("concentration", "top 25", "top25", "集中度"),
    "ai": ("ai", "artificial intelligence", "人工智能", "人工智慧"),
    "semiconductor": ("semiconductor", "semiconductors", "chip", "chips", "半导体", "芯片", "晶片"),
    "inflation": ("inflation", "cpi", "通胀", "通货膨胀"),
    "earnings": ("earnings", "profits", "盈利", "获利", "财报"),
}
_STOP = {"the", "and", "for", "with", "from", "this", "that", "what", "how", "are", "into", "about",
         "research", "report", "analysis", "compare", "current", "最新", "研究", "分析", "报告", "比较", "情况"}
_DECIMAL = re.compile(r"[+-]?\d{1,60}(?:\.\d{1,30})?\Z")
_NUMBERS = re.compile(r"(?<![A-Za-z0-9_.,])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z0-9_]|[.,]\d)")
_SOURCE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_DEFINITIONS = (
    ("sector_positioning", "行業持倉與擁擠度", ("sector_pack",), "需行業持倉或投資者倉位數據；基金流量不能替代持倉。"),
    ("margin", "FINRA 證券保證金餘額", ("finra",), "需 FINRA 借方餘額及觀測月份；總額不能當作行業槓桿。"),
    ("fund_borrowing", "SEC 私募基金借款、槓桿與集中度", ("sec_pf",), "需 SEC 私募基金借款或槓桿，以及集中度的直接數據。"),
    ("historical_comparison", "同口徑歷史比較", (), "需同指標、單位、樣本、數據集與頻率的至少兩期資料。"),
)


def _text(value, limit=MAX_CONTENT):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", _text(value)).casefold().translate(_TRADITIONAL).split())


def _phrase(value):
    return " ".join(re.sub(r"[_-]+", " ", _normal(value)).split())


def _contains(text, term):
    if not term:
        return False
    # Latin word boundaries keep AI from matching 'said', 'retail' or 'chair'.
    before = r"(?<![a-z0-9])" if term[0].isascii() and term[0].isalnum() else ""
    after = r"(?![a-z0-9])" if term[-1].isascii() and term[-1].isalnum() else ""
    return re.search(before + re.escape(term) + after, text) is not None


def _concept(text, key):
    return any(_contains(text, word) for word in _CONCEPTS[key])


def _terms(text):
    normalized = _normal(text)
    terms = []
    for word in re.findall(r"[a-z][a-z0-9]*(?:[.][a-z0-9]+)*|[" + _HAN + r"]+", normalized):
        if word in _STOP:
            continue
        if re.fullmatch(r"[" + _HAN + r"]+", word):
            if len(word) <= 6:
                terms.append(word)
            terms.extend(word[i:i + 2] for i in range(min(len(word) - 1, 80)) if word[i:i + 2] not in _STOP)
        elif len(word) >= 2:
            terms.append(word)
    return list(dict.fromkeys(terms))[:80]


def rank_articles(articles, topic, keywords="", *, include_unrelated=False):
    """Rank topic matches stably, retaining the original article dictionaries.

    Additional keywords supplement the topic. Scores are relevance hints, never
    evidence verification or a claim that differently worded stories are one event.
    """
    if not isinstance(articles, (list, tuple)):
        return []
    topic_text = _normal(_text(topic, 500))
    topic_terms, extra_terms = _terms(topic_text), _terms(_text(keywords, 1000))
    concepts = [key for key in _CONCEPTS if _concept(topic_text + " " + _normal(keywords), key)]
    ranked = []
    for index, article in enumerate(articles[:MAX_ITEMS]):
        if not isinstance(article, dict):
            continue
        fields = [(_normal(article.get(field)), weight) for field, weight in (("title", 4), ("summary", 2), ("content", 1))]
        score, reasons = 0, []
        for term in list(dict.fromkeys(topic_terms + extra_terms)):
            hits = sum(weight for text, weight in fields if _contains(text, term))
            if hits:
                score += hits * (2 if term in extra_terms else 1)
                reasons.append("主題詞：" + term)
        for key in concepts:
            hits = sum(weight for text, weight in fields if _concept(text, key))
            if hits:
                score += hits * 3
                reasons.append("相關概念：" + key)
        if topic_text and len(topic_text) > 2:
            score += sum(weight * 2 for text, weight in fields if _contains(text, topic_text))
        if score > 0 or include_unrelated:
            ranked.append({"article": article, "score": score, "reasons": reasons[:12], "_index": index})
    ranked.sort(key=lambda item: (-item["score"], item["_index"]))
    for item in ranked:
        item.pop("_index")
    return ranked


def _period(value):
    value = _text(value, 32)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return "day", date.fromisoformat(value)
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
            return "month", date(year, month, calendar.monthrange(year, month)[1])
        if re.fullmatch(r"\d{4}Q[1-4]", value):
            year, quarter = int(value[:4]), int(value[-1])
            month = quarter * 3
            return "quarter", date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        pass
    return None


def _as_of(value):
    if value is None:
        return datetime.now(timezone(timedelta(hours=8))).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _period_anchored(period, excerpt, source):
    frequency, end = _period(period)
    text = _normal(excerpt)
    if _contains(text, period.casefold()):
        return True
    if any(label == period for _, _, label in _period_spans(text, frequency)):
        return True
    observed = _period(source.get("observation_date"))
    if observed and (observed[1] == end or (frequency == "month" and observed[1].year == end.year and observed[1].month == end.month)):
        return True
    year, month = end.year, end.month
    if frequency == "day":
        return bool(re.search(fr"{year}年\s*0?{month}月\s*0?{end.day}日", text))
    if frequency == "quarter":
        quarter = month // 3
        return any(re.search(pattern, text) for pattern in (
            fr"\b{year}\s*q{quarter}\b", fr"\bq{quarter}\s*{year}\b", fr"{year}年?第?\s*{quarter}\s*(?:季|季度)"))
    names = (calendar.month_abbr[month].lower(), calendar.month_name[month].lower())
    return (any(re.search(fr"\b{name}[- ](?:{year}|{str(year)[2:]})\b", text) for name in names)
            or bool(re.search(fr"{year}年\s*0?{month}月", text)))


def _period_spans(text, frequency):
    """Find explicit periods without interpreting a table's column layout."""
    spans = []

    def add(start, end, year, month, day=None):
        try:
            if frequency == "day" and day is not None:
                label = date(year, month, day).isoformat()
            elif frequency == "month":
                date(year, month, 1)
                label = f"{year:04d}-{month:02d}"
            else:
                return
            spans.append((start, end, label))
        except ValueError:
            pass

    if frequency == "quarter":
        for pattern, year_group, quarter_group in ((r"\b(\d{4})\s*q([1-4])\b", 1, 2),
                                                    (r"\bq([1-4])\s*(\d{4})\b", 2, 1),
                                                    (r"(\d{4})年?第?\s*([1-4])\s*(?:季度|季)", 1, 2)):
            spans.extend((m.start(), m.end(), f"{int(m[year_group]):04d}Q{m[quarter_group]}") for m in re.finditer(pattern, text))
        return sorted(spans)
    for match in re.finditer(r"(?<!\d)(\d{4})-(\d{2})(?:-(\d{2}))?(?!\d)", text):
        add(match.start(), match.end(), int(match[1]), int(match[2]), int(match[3]) if match[3] else None)
    for match in re.finditer(r"(\d{4})年\s*(\d{1,2})月(?:\s*(\d{1,2})日)?", text):
        add(match.start(), match.end(), int(match[1]), int(match[2]), int(match[3]) if match[3] else None)
    months = {name.casefold(): index for index in range(1, 13) for name in (calendar.month_abbr[index], calendar.month_name[index])}
    names = "|".join(months)
    for match in re.finditer(r"\b(" + names + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        add(match.start(), match.end(), int(match[3]), months[match[1]], int(match[2]))
    for match in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + names + r")\s+(\d{4})\b", text):
        add(match.start(), match.end(), int(match[3]), months[match[2]], int(match[1]))
    if frequency == "month":
        pattern = r"\b(" + "|".join(months) + r")[- ](\d{4}|\d{2})\b"
        for match in re.finditer(pattern, text):
            year = int(match[2]) + (2000 if len(match[2]) == 2 else 0)
            add(match.start(), match.end(), year, months[match[1]])
    return sorted(spans)


def _period_value_paired(period, number, excerpt, labels=()):
    """Reject a number borrowed from another period in a broad excerpt.

    A header with multiple periods over unlabeled numeric columns is ambiguous.
    It stays a candidate until the user supplies a narrower, intelligible row.
    """
    frequency, _ = _period(period)
    text = "\n".join(_normal(line) for line in excerpt.splitlines())
    spans = _period_spans(text, frequency)
    distinct = {label for _, _, label in spans}
    if not distinct:
        return True  # The source observation_date can supply the period.
    if period not in distinct:
        return False
    if len(distinct) == 1:
        return True
    clauses = re.split(r"[;；\n。]|\band\b|以及|而|及", text)
    for clause in clauses:
        local_periods = {label for _, _, label in _period_spans(clause, frequency)}
        if local_periods == {period}:
            without_dates = clause
            for start, end, _ in reversed(_period_spans(clause, frequency)):
                without_dates = without_dates[:start] + " " * (end - start) + without_dates[end:]
            if _number_anchored(number, without_dates, labels=labels):
                return True
    return False


def _unit_anchored(unit, text):
    unit, text = _phrase(unit), _phrase(text)
    if _contains(text, unit):
        return True
    aliases = {
        "%": ("%", "percent", "percentage", "百分比"),
        "percent": ("%", "percent", "percentage", "百分比"),
        "usd million": ("usd million", "usd millions", "$ million", "$ millions", "百万美元"),
        "usd billion": ("usd billion", "usd billions", "$ billion", "$ billions", "十亿美元"),
        "usd": ("usd", "us dollar", "美元"),
        "ratio": ("ratio", "倍"),
    }
    return any(_contains(text, alias) for alias in aliases.get(unit, ()))


def _metric_anchored(metric, excerpt):
    metric, text = _phrase(metric), _phrase(excerpt)
    if not _contains(text, metric):
        return False
    # Preserve an explicit accounting qualifier in the selected metric. Without
    # it, identical 'borrowing' keys could combine gross and net data.
    prefix = r"(?:\b(?:gross|net|long|short|total|aggregate)|净|总|毛|多头|空头|净额|总额)\s*$"
    suffix = r"^\s*(?:\b(?:flows?|stocks?|balances?)\b|存量|流量|余额|净额|总额)"
    for match in re.finditer(re.escape(metric), text):
        if re.search(prefix, text[max(0, match.start() - 30):match.start()]) or re.search(suffix, text[match.end():match.end() + 30]):
            return False
    return True


def _decimal(value):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value.strip()):
        return None
    try:
        number = Decimal(value.strip())
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _decimal_text(number):
    if not number:
        return "0"
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _number_anchored(number, excerpt, *, labels=()):
    text = _normal(excerpt)
    # A year, month or day is metadata, never the measured quantity. Strip all
    # period representations, even when they differ from the selected frequency.
    spans = {(start, end) for frequency in ("day", "month", "quarter")
             for start, end, _ in _period_spans(text, frequency)}
    masked = list(text)
    for start, end in spans:
        masked[start:end] = " " * (end - start)
    text = "".join(masked)
    # 'Top 25 concentration' and an index or sample label can contain numbers
    # that describe the metric/population, rather than its observed value.
    for label in labels:
        normalized = _normal(label)
        if normalized:
            text = text.replace(normalized, " ")
    for token in _NUMBERS.findall(text):
        try:
            if Decimal(token.replace(",", "")) == number:
                return True
        except InvalidOperation:
            pass
    return False


def _warning(code, message, observation=None):
    return {"code": code, "message": message, "observation": observation}


def _source_context(source):
    return _normal(" ".join(_text(source.get(key)) for key in ("title", "publisher", "url", "content")))


def _sec_pf(text):
    authority = any(_contains(text, word) for word in ("sec", "美国证监会", "美国证券交易委员会", "美国证交会"))
    dataset = any(_contains(text, word) for word in ("private fund", "private funds", "form pf", "hedge fund", "hedge funds", "私募基金", "合资格对冲基金"))
    return authority and dataset


def _direct_kind(observation, source):
    text = _normal(" ".join(observation[key] for key in ("metric", "scope")))
    context = _source_context(source)
    kind = observation["requirement_id"]
    if kind == "sector_positioning":
        flow_metric = re.search(r"\bflows?\b|流量|资金流|净流", _normal(observation["metric"]))
        benchmark = re.search(r"\b(?:etfs?|index|benchmark)\b|指数|基准", text)
        return (_concept(text, "sector") and _concept(_normal(observation["metric"]), "positioning")
                and not flow_metric and not benchmark)
    if kind == "margin":
        return _contains(context, "finra") and any(_contains(text, marker) for marker in (
            "margin debt", "margin balance", "margin balances", "debit balance", "debit balances", "借方余额", "保证金余额"))
    if kind == "fund_borrowing":
        return _sec_pf(context) and any(_concept(text, key) for key in ("borrowing", "leverage", "concentration"))
    return True


def _observations(raw, sources, requirement_ids, topic, as_of, warnings):
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append(_warning("invalid_observations", "數據列格式不正確，未納入研究。"))
        return []
    if len(raw) > MAX_OBSERVATIONS:
        warnings.append(_warning("observation_limit", "數據列超過上限，只處理首 200 列。"))
    cleaned, seen = [], set()
    for index, item in enumerate(raw[:MAX_OBSERVATIONS], 1):
        code, message = "invalid_observation", "數據列需要有效來源、指標、數值、單位、期間、樣本及原文摘錄。"
        valid = isinstance(item, dict)
        fields = {}
        for key, maximum in (("source_id", 64), ("requirement_id", 40), ("metric", 160), ("unit", 80),
                             ("period", 32), ("scope", 240), ("evidence_text", MAX_EXCERPT)):
            value = item.get(key) if valid else None
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                valid = False
            else:
                fields[key] = value.strip()
        if not valid:
            warnings.append(_warning(code, message, index))
            continue
        number, parsed_period = _decimal(item.get("value")), _period(fields["period"])
        source = sources.get(fields["source_id"])
        if source is None or fields["requirement_id"] not in requirement_ids:
            code, message = "unknown_reference", "數據列引用了本次不存在的來源或證據要求。"
        elif number is None or parsed_period is None:
            code, message = "invalid_value_period", "數值須為有限十進制字串；期間須使用有效日期、月份或季度。"
        elif as_of is None or parsed_period[1] > as_of:
            code, message = "future_observation", "數據期間晚於資料截至日，或截至日無效，未納入研究。"
        elif fields["evidence_text"] not in _text(source.get("content")):
            code, message = "unanchored_excerpt", "摘錄不是來源正文中的完整原句，未納入研究。"
        elif not _number_anchored(number, fields["evidence_text"], labels=(fields["metric"], fields["scope"], fields["unit"], item.get("group"))):
            code, message = "unanchored_value", "來源摘錄中找不到此數值，未納入研究。"
        elif not _period_value_paired(fields["period"], number, fields["evidence_text"], labels=(fields["metric"], fields["scope"], fields["unit"], item.get("group"))):
            code, message = "ambiguous_period_value", "摘錄包含多期數據，無法確定數值與期間對應；請提供同一行或同一句的清楚摘錄。"
        elif re.search(r"\b(?:missing|unavailable|not available|not provided|no data)\b|缺失|缺少|未提供|没有数据|无数据|尚未取得", _normal(fields["evidence_text"])):
            code, message = "missing_data_excerpt", "摘錄明示數據缺失，不能作為已齊備的直接證據。"
        elif not (_metric_anchored(fields["metric"], fields["evidence_text"])
                  and _contains(_phrase(fields["evidence_text"]), _phrase(fields["scope"]))
                  and _unit_anchored(fields["unit"], fields["evidence_text"])
                  and _period_anchored(fields["period"], fields["evidence_text"], source)):
            code, message = "unanchored_metadata", "摘錄未支持指標、單位、期間或樣本口徑，未納入研究。"
        elif not _direct_kind(fields, source):
            code, message = "wrong_evidence_kind", "數據口徑不符合此項證據要求，未納入研究。"
        elif fields["requirement_id"] == "generic_data" and not rank_articles([
                {"title": fields["metric"] + " " + fields["scope"], "content": fields["evidence_text"]}], topic):
            code, message = "unrelated_observation", "數據與研究主題沒有可辨識的關聯，未納入研究。"
        else:
            group = item.get("group")
            if isinstance(group, str) and not group.strip():
                group = None
            if group is not None and (not isinstance(group, str) or not 1 <= len(group.strip()) <= 160
                                      or not _contains(_phrase(fields["evidence_text"]), _phrase(group))):
                warnings.append(_warning("unanchored_group", "數據集標籤不在摘錄中，未納入研究。", index))
                continue
            fields["value"] = _decimal_text(number)
            if group is not None:
                fields["group"] = group.strip()
            identity = tuple(fields.get(key, "") for key in ("source_id", "requirement_id", "metric", "unit", "period", "scope", "group", "value"))
            if identity not in seen:
                cleaned.append(fields)
                seen.add(identity)
            continue
        warnings.append(_warning(code, message, index))
    return cleaned


def _comparisons(observations, warnings):
    groups = defaultdict(list)
    for item in observations:
        # An explicit, quoted dataset label is required to compare two sources.
        dataset = _phrase(item["group"]) if item.get("group") else "source:" + item["source_id"]
        key = tuple(_phrase(item[field]) for field in ("metric", "unit", "scope")) + (dataset, _period(item["period"])[0])
        groups[key].append(item)
    comparisons = []
    for records in groups.values():
        by_period = defaultdict(list)
        for item in records:
            by_period[item["period"]].append(item)
        if any(len({item["value"] for item in rows}) > 1 for rows in by_period.values()):
            warnings.append(_warning("conflicting_values", "同口徑同期間有衝突數值，該數據組未作比較。"))
            continue
        series = sorted((rows[0] for rows in by_period.values()), key=lambda item: _period(item["period"])[1])
        for before, after in zip(series, series[1:]):
            with localcontext() as context:
                context.prec = 100
                base, end = Decimal(before["value"]), Decimal(after["value"])
                change = end - base
                percentage = (change / abs(base) * 100).quantize(Decimal("0.000001")) if base else None
            comparisons.append({"metric": after["metric"], "unit": after["unit"], "scope": after["scope"],
                "group": after.get("group", ""), "from_period": before["period"], "to_period": after["period"],
                "from_value": before["value"], "to_value": after["value"], "change": _decimal_text(change),
                "percent_change": _decimal_text(percentage) if percentage is not None else None,
                "source_ids": list(dict.fromkeys((before["source_id"], after["source_id"]))),
                "operands": [{key: row[key] for key in ("source_id", "period", "value")} for row in (before, after)],
                "formula": "change = to_value - from_value; percent_change = change / abs(from_value) * 100",
                "requirement_ids": list(dict.fromkeys((before["requirement_id"], after["requirement_id"])))})
    return comparisons


def _candidate(kind, source, topic):
    text = _source_context(source)
    if kind == "sector_positioning":
        return _concept(text, "sector") and (_concept(text, "positioning") or "flow" in text or "流量" in text)
    if kind == "margin":
        return _contains(text, "finra") or _concept(text, "margin")
    if kind == "fund_borrowing":
        return _sec_pf(text) or any(_contains(text, word) for word in ("private fund", "private funds", "私募基金", "form pf"))
    if kind == "historical_comparison":
        return False
    return bool(rank_articles([source], topic))


def build_research_brief(topic, sources, *, observations=None, as_of=None):
    """Return a bounded JSON-safe checklist, validated records and comparisons.

    Historical arithmetic uses only compatible frequencies and explicitly
    identified populations. Monthly/quarterly observations are complete periods;
    their period end must be on or before the supplied cutoff date.
    """
    topic = _text(topic, 500)
    normalized = _normal(topic)
    has_crowding, has_leverage = _concept(normalized, "crowding"), _concept(normalized, "leverage")
    us_equity = any(_contains(normalized, marker) for marker in (
        "美股", "美国股票", "美国股市", "us equity", "us equities", "u.s. equity", "u.s. equities", "us stocks", "u.s. stocks", "s&p 500"))
    crowding = (has_crowding and has_leverage) or (us_equity and (has_crowding or has_leverage))
    definitions = _DEFINITIONS if crowding else (("generic_data", "主題直接數據與來源", (), "需與主題相關、可追溯且口徑明確的直接數據。"),)
    warnings, source_map = [], {}
    if isinstance(sources, (list, tuple)):
        for source in sources[:MAX_ITEMS]:
            if isinstance(source, dict) and isinstance(source.get("id"), str) and _SOURCE_ID.fullmatch(source["id"]):
                if source["id"] in source_map:
                    warnings.append(_warning("duplicate_source_id", "來源編號重複，未使用重複來源。"))
                else:
                    source_map[source["id"]] = source
    cutoff = _as_of(as_of)
    if cutoff is None:
        warnings.append(_warning("invalid_as_of", "資料截至日無效，未核准任何觀測數據。"))
    requirement_ids = {item[0] for item in definitions}
    # Historical comparison is derived, so rows must belong to a direct evidence requirement.
    cleaned = _observations(observations, source_map, requirement_ids - {"historical_comparison"}, topic, cutoff, warnings)
    comparisons = _comparisons(cleaned, warnings)
    requirements, recommended = [], []
    for kind, label, catalog_keys, missing_reason in definitions:
        matching = [item for item in cleaned if item["requirement_id"] == kind]
        identifiers = list(dict.fromkeys(item["source_id"] for item in matching))
        satisfied = bool(matching)
        if kind == "fund_borrowing":
            texts = [_normal(item["metric"]) for item in matching]
            satisfied = (any((_concept(text, "borrowing") or _concept(text, "leverage")) and not _concept(text, "concentration") for text in texts)
                         and any(_concept(text, "concentration") for text in texts))
        elif kind == "historical_comparison":
            satisfied = bool(comparisons)
            identifiers = list(dict.fromkeys(identifier for item in comparisons for identifier in item["source_ids"]))
        candidates = [identifier for identifier, source in source_map.items() if _candidate(kind, source, topic)]
        status = "fulfilled" if satisfied else "candidate" if candidates or matching else "missing"
        reason = "已取得原文支持且口徑明確的數據；仍需實質審稿核對來源。" if satisfied else missing_reason
        if status == "candidate":
            reason += " 已找到可能相關的材料，但標題或關鍵詞不等於直接證據。"
        requirements.append({"id": kind, "label": label, "status": status,
            "source_ids": identifiers if satisfied else list(dict.fromkeys(identifiers + candidates)),
            "reason": reason, "catalog_keys": list(catalog_keys)})
        if not satisfied:
            recommended.extend(catalog_keys)
    missing = [item["id"] for item in requirements if item["status"] != "fulfilled"]
    return {"profile": {"key": "crowding_leverage" if crowding else "generic", "label": "擁擠交易與槓桿研究" if crowding else "一般主題研究"},
            "requirements": requirements, "missing_required": missing,
            "recommended_catalog_keys": list(dict.fromkeys(recommended)), "ready": not missing,
            "observations": cleaned, "comparisons": comparisons, "warnings": warnings}
