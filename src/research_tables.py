"""Deterministic observations from a narrowly recognised official FINRA table.

Only the canonical dollar-millions margin-statistics layout is supported.
All proposed records still pass the existing evidence/period/unit validators;
parsing establishes traceability and never independent factual verification.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .research_quality import MAX_EXCERPT, _as_of, _period, build_research_brief


_METRIC = "Debit Balances in Customers' Securities Margin Accounts"
_SCOPE = "Customers' Securities Margin Accounts"
_UNIT = "$ millions"
_COLUMNS = ("Month/Year", _METRIC, "Free Credit Balances in Customers' Cash Accounts",
            "Free Credit Balances in Customers' Securities Margin Accounts")
_SPACE = r"[ \t\u00a0]*"
_TABLE_START = re.compile(
    r"(?m)^" + _SPACE + r"FINRA Statistics1?" + _SPACE + r"\(shown in \$ millions\)" + _SPACE +
    r"\r?\n(?:" + _SPACE + r"\r?\n)*" + _SPACE + r"\|" + _SPACE +
    (_SPACE + r"\|" + _SPACE).join(re.escape(column) for column in _COLUMNS) +
    _SPACE + r"\|?" + _SPACE + r"(?:\r?\n|$)")
_NUMBER = r"(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)"
_ROW = re.compile(
    r"^" + _SPACE + r"\|" + _SPACE + r"(?P<period>[A-Z][a-z]{2}-\d{2})" + _SPACE +
    r"\|" + _SPACE + r"(?P<debit>" + _NUMBER + r")" + _SPACE +
    r"\|" + _SPACE + _NUMBER + _SPACE + r"\|" + _SPACE + _NUMBER + _SPACE + r"\|?" + _SPACE + r"$")
_MONTHS = {name: index for index, name in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _official_finra(source):
    if source.get("source_type") != "official" or not isinstance(source.get("url"), str):
        return False
    try:
        parts = urlsplit(source["url"])
        return (parts.scheme == "https" and parts.hostname in {"www.finra.org", "finra.org"}
                and parts.username is None and parts.password is None and parts.port in {None, 443})
    except ValueError:
        return False


def extract_table_observations(sources, as_of):
    """Return locally accepted FINRA debit-balance rows, without mutating sources.

    Each excerpt spans the original unit/header through its original data row,
    so a July observation does not borrow its date from August's observation_date.
    Two-digit years use the existing validator's 2000-based month convention;
    complete periods later than the explicit cutoff are omitted.
    """
    cutoff = _as_of(as_of) if as_of is not None else None
    if cutoff is None or not isinstance(sources, (list, tuple)):
        return []
    records = []
    for source in sources[:40]:
        if (not isinstance(source, dict) or not _official_finra(source)
                or not isinstance(source.get("id"), str) or not isinstance(source.get("content"), str)):
            continue
        content = source["content"]
        starts = list(_TABLE_START.finditer(content))
        # Multiple canonical tables may describe revisions/different populations.
        # Do not silently choose one and manufacture a comparable dataset.
        if len(starts) != 1:
            continue
        table = starts[0]
        proposed, cursor = [], table.end()
        for line in content[cursor:].splitlines(keepends=True)[:120]:
            cursor += len(line)
            if not line.strip():
                continue
            row = _ROW.fullmatch(line.rstrip("\r\n"))
            if not row:
                break
            name, year = row["period"].split("-")
            if name not in _MONTHS:
                break
            period = f"{2000 + int(year):04d}-{_MONTHS[name]:02d}"
            if _period(period)[1] > cutoff:
                continue
            excerpt = content[table.start():cursor].rstrip("\r\n")
            if len(excerpt) > MAX_EXCERPT:
                break
            proposed.append({"source_id": source["id"], "requirement_id": "margin",
                "metric": _METRIC, "scope": _SCOPE, "unit": _UNIT, "period": period,
                "value": row["debit"].replace(",", ""), "evidence_text": excerpt})
        if proposed:
            accepted = build_research_brief("Crowding and leverage", [source],
                observations=proposed, as_of=cutoff)["observations"]
            records.extend(accepted)
    return records
