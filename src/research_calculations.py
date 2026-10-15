"""Small, evidence-bound financial calculations. No network, model math or eval.

Requests select a named formula and evidence IDs. NPV is explicitly a scenario:
its assumptions are saved alongside its result and never establish actual ROI.
Numbers are decimal strings so saved results can be independently reproduced.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re
import unicodedata


MAX_REQUESTS = 40
MAX_YEARS = 50
_NUMBER = re.compile(r"[+-]?\d{1,40}(?:\.\d{1,20})?\Z")
_ROLES = {
    "ocf": {"ocf", "operating cash flow", "operating cash flows", "cash flow from operations", "cash flows from operating activities",
            "net cash from operating activities", "net cash flows from operating activities",
            "net cash generated from operating activities", "net cash generated from operations",
            "net cash provided by operating activities", "经营现金流", "經營現金流", "經營現金流量",
            "经营活动产生的现金流量净额", "經營活動產生的現金流量淨額", "經營業務現金淨額"},
    "capex": {"capex", "capital expenditure", "capital expenditures", "cash capital expenditures",
              "purchases of property and equipment", "purchases of property, plant and equipment",
              "资本开支", "資本開支", "资本支出", "資本支出", "现金资本开支", "現金資本開支"},
}


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("missing_or_invalid_number")
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        raise ValueError("missing_or_invalid_number")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("missing_or_invalid_number") from None
    if not number.is_finite():
        raise ValueError("missing_or_invalid_number")
    return number


def _number(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def metric_role(metric):
    """Exact named accounting roles; revenue cannot become operating cash flow."""
    name = _normal(metric).replace("_", " ")
    for role, aliases in _ROLES.items():
        if name in {_normal(alias) for alias in aliases}:
            return role
    return name


def _period_shape(value):
    value = _normal(value).upper().replace(" ", "")
    # Explicit fiscal/calendar prefixes remain distinct. Ambiguous labels are
    # not guessed, and unequal-duration/quarter/full-year pairs cannot compare.
    for pattern, shape in (
        (r"FY(\d{4})", "fiscal_year"), (r"CY(\d{4})", "calendar_year"),
        (r"(\d{4})", "year"), (r"(\d{4})-Q([1-4])", "quarter"),
        (r"(\d{4})Q([1-4])", "quarter"), (r"(\d{4})-H([12])", "half_year"),
        (r"(\d{4})-(0[1-9]|1[0-2])", "month"),
    ):
        match = re.fullmatch(pattern, value)
        if match:
            return shape, tuple(int(part) for part in match.groups())
    return None


def _operand(identifier, evidence):
    if not isinstance(identifier, str) or identifier not in evidence:
        raise ValueError("missing_evidence")
    item = evidence[identifier]
    if (item.get("kind") != "numeric" or item.get("eligible") is not True
            or item.get("traceability") != "anchored" or item.get("conflict")):
        raise ValueError("ineligible_evidence")
    if item.get("statement_type") not in {"actual", "guidance", "forecast"}:
        raise ValueError("unsupported_statement_type")
    required = ("source_id", "entity", "metric", "unit", "period", "scope")
    if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
        raise ValueError("missing_comparability_metadata")
    value = _decimal(item.get("value"))
    return {"evidence_id": identifier, **{key: item[key] for key in required},
            "statement_type": item["statement_type"], "value": _number(value)}, value


def _compatible(left, right, *, period_change=False):
    for field in ("entity", "unit", "scope", "statement_type"):
        if _normal(left[field]) != _normal(right[field]):
            raise ValueError("incompatible_" + field)
    if period_change:
        before, after = _period_shape(left["period"]), _period_shape(right["period"])
        if not before or not after or before[0] != after[0] or before[1] >= after[1]:
            raise ValueError("incompatible_period")
    elif _normal(left["period"]) != _normal(right["period"]):
        raise ValueError("incompatible_period")


def _scenario(request):
    assumptions = request.get("assumptions")
    if not isinstance(assumptions, dict):
        raise ValueError("missing_scenario_assumptions")
    for key in ("basis", "unit", "entity", "scope", "period"):
        if not isinstance(assumptions.get(key), str) or not assumptions[key].strip():
            raise ValueError("missing_scenario_" + key)
    years = assumptions.get("years")
    flows = assumptions.get("cash_flows")
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= MAX_YEARS:
        raise ValueError("invalid_scenario_years")
    if not isinstance(flows, list) or len(flows) != years:
        raise ValueError("missing_scenario_cash_flows")
    investment = _decimal(assumptions.get("investment"))
    rate = _decimal(assumptions.get("discount_rate"))
    if investment <= 0 or not Decimal("-1") < rate <= Decimal("10"):
        raise ValueError("invalid_scenario_investment_or_rate")
    values = [_decimal(value) for value in flows]
    # Every year is explicit. No missing cash flow, residual or maintenance cost
    # is invented; all net flows, including any terminal proceeds, belong here.
    discounted = [value / ((1 + rate) ** year) for year, value in enumerate(values, 1)]
    result = sum(discounted, Decimal(0)) - investment
    inputs = {key: assumptions[key] for key in ("basis", "unit", "entity", "scope", "period")}
    inputs.update(investment=_number(investment), cash_flows=[_number(v) for v in values],
                  discount_rate=_number(rate), discount_rate_unit="decimal", years=years,
                  cash_flow_timing="year_end", terminal_value="included_in_explicit_cash_flows_only")
    return inputs, {"npv": _number(result), "discounted_cash_flows": [_number(v) for v in discounted]}


def calculate_requests(requests, evidence_items):
    """Calculate up to 40 named requests, retaining failures as explicit records.

    delta/percent_change: operand_ids={base:E1,current:E2}; different periods
    require comparison_mode='period_change'. cashflow_coverage: {ocf:E1,capex:E2}.
    npv: assumptions={investment,cash_flows,discount_rate,years,unit,entity,scope,
    period,basis}; rate is a decimal fraction and flows are net year-end flows.
    Optional evidence_ids on a scenario document the basis, not actual returns.
    """
    if not isinstance(requests, list) or len(requests) > MAX_REQUESTS:
        raise ValueError("invalid_calculation_requests")
    if not isinstance(evidence_items, list):
        raise ValueError("invalid_evidence_items")
    evidence = {}
    for item in evidence_items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            if item["id"] in evidence:
                raise ValueError("duplicate_evidence_id")
            evidence[item["id"]] = item
    records, seen = [], set()
    for index, request in enumerate(requests, 1):
        request = request if isinstance(request, dict) else {}
        identifier = request.get("id", f"C{index}")
        if not isinstance(identifier, str) or not re.fullmatch(r"C[1-9][0-9]{0,5}", identifier) or identifier in seen:
            raise ValueError("invalid_calculation_id")
        seen.add(identifier)
        kind = request.get("kind")
        record = {"id": identifier, "kind": kind if isinstance(kind, str) else "unknown",
                  "status": "unsupported", "reason": "", "inputs": {}, "evidence_ids": [],
                  "source_ids": [], "formula": "", "result": None,
                  "independently_verified": False}
        try:
            with localcontext() as context:
                context.prec = 50
                if kind == "npv":
                    ids = request.get("evidence_ids", [])
                    if (not isinstance(ids, list) or any(not isinstance(i, str) or i not in evidence for i in ids)):
                        raise ValueError("missing_evidence")
                    if any(evidence[i].get("traceability") != "anchored" or evidence[i].get("conflict") for i in ids):
                        raise ValueError("ineligible_evidence")
                    record["evidence_ids"] = list(dict.fromkeys(ids))
                    record["source_ids"] = list(dict.fromkeys(evidence[i]["source_id"] for i in ids if evidence[i].get("source_id")))
                    inputs, result = _scenario(request)
                    record.update(inputs=inputs, result=result, statement_type="assumption", value_type="scenario",
                                  formula="NPV = -investment + sum(cash_flows[t] / (1 + discount_rate) ** (t + 1))",
                                  limitation="情景計算；不代表實際回報。各年淨現金流須已包含相關成本及任何期末回收額。",
                                  **{key: inputs[key] for key in ("entity", "unit", "period", "scope")})
                elif kind in {"delta", "percent_change", "cashflow_coverage"}:
                    operands = request.get("operand_ids")
                    keys = ("ocf", "capex") if kind == "cashflow_coverage" else ("base", "current")
                    if not isinstance(operands, dict) or set(operands) != set(keys):
                        raise ValueError("missing_operands")
                    values = {}
                    for key in keys:
                        record["inputs"][key], values[key] = _operand(operands[key], evidence)
                    items = [record["inputs"][key] for key in keys]
                    record["evidence_ids"] = list(dict.fromkeys(item["evidence_id"] for item in items))
                    record["source_ids"] = list(dict.fromkeys(item["source_id"] for item in items))
                    mode = request.get("comparison_mode", "same_period")
                    if mode not in {"same_period", "period_change"} or kind == "cashflow_coverage" and mode != "same_period":
                        raise ValueError("unsupported_comparison_mode")
                    _compatible(*items, period_change=mode == "period_change")
                    record.update({key: items[0][key] for key in ("entity", "unit", "scope", "statement_type")})
                    record["period"] = [item["period"] for item in items] if mode == "period_change" else items[0]["period"]
                    record["value_type"] = record["statement_type"]
                    if kind == "cashflow_coverage":
                        if metric_role(items[0]["metric"]) != "ocf" or metric_role(items[1]["metric"]) != "capex":
                            raise ValueError("incompatible_metric_role")
                        if values["ocf"] <= 0 or values["capex"] <= 0:
                            raise ArithmeticError("nonpositive_cashflow_or_capex")
                        record["formula"] = "coverage_ratio = ocf / capex; fcf = ocf - capex"
                        record["result"] = {"coverage_ratio": _number(values["ocf"] / values["capex"]),
                                            "fcf": _number(values["ocf"] - values["capex"])}
                        record["result_units"] = {"coverage_ratio": "multiple", "fcf": record["unit"]}
                        record["limitation"] = "本計算的 FCF 定義為 OCF 減所列現金資本開支；資金覆蓋能力不等於新增投資回報。"
                    else:
                        if metric_role(items[0]["metric"]) != metric_role(items[1]["metric"]):
                            raise ValueError("incompatible_metric")
                        change = values["current"] - values["base"]
                        record["metric"] = items[0]["metric"]
                        if kind == "delta":
                            record["formula"] = "delta = current - base"
                            record["result"] = {"delta": _number(change)}
                            record["result_units"] = {"delta": record["unit"]}
                        else:
                            if values["base"] <= 0:
                                raise ArithmeticError("nonpositive_percentage_base")
                            record["formula"] = "percent_change = (current - base) / base * 100"
                            record["result"] = {"percent_change": _number(change / values["base"] * 100)}
                            record["result_units"] = {"percent_change": "%"}
                else:
                    raise ValueError("unsupported_formula")
                record["status"] = "computed"
        except ArithmeticError as error:
            record.update(status="not_applicable", reason=str(error), result=None)
        except (ValueError, TypeError, KeyError) as error:
            record.update(status="unsupported", reason=str(error), result=None)
        records.append(record)
    return records
