"""DeepSeek writes report narrative from computed facts; every figure is verified.

The model only rephrases observations computed by sparkx.analysis. A reply that
contains a number, date or store absent from those facts is rejected (one retry
with the offending tokens named), and the rule-based text is used instead.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
import logging
import re

_LOG = logging.getLogger(__name__)
_DATE = re.compile(r"(?<!\d)(?:\d{4}-)?(\d{1,2})-(\d{1,2})(?!\d)|(?<!\d)(\d{1,2})月(\d{1,2})日")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[-+]?\$?(\d[\d,]*(?:\.\d+)?)")
_FORBIDDEN = re.compile(r"https?://|www\.|<at\b|<[a-z/]|\]\(|```|@所有人|@all", re.IGNORECASE)
_CJK = re.compile(r"[\u4e00-\u9fff]")

DAILY_PROMPT = """你是资深亚马逊广告投手，为老板和运营写 SparkX 广告日报底部的「须知」。
只能使用用户 JSON 里 facts 与 tables 出现过的数字、日期和店铺名，并原样照抄（含 $、%、逗号和小数位）；
不得换算、估算、加总或改写成“万/k”，不得出现任何新的数字、日期或店铺。
口吻直接、口语化，像在群里提醒老板：先下结论，再给证据，可以用“——”连接。不写开场白、标题党或总结。
优先级：站点或授权缺数据 > 花费集中与 ACOS 分化 > 零出单、高 ACOS 店铺 > 效率最好的店铺。
不臆测没有证据的原因；每条最多给一句可执行建议（核对 Token 授权范围、看搜索词、降出价、控预算等）。
只输出 json：{"notes":[{"title":"不超过16字","text":"不超过180字"}]}，2 到 3 条。"""

REVIEW_PROMPT = """你是资深亚马逊广告投手，为 SparkX 广告异常复盘写「最严重」判断和「建议动作」。
只能使用用户 JSON 里 facts 出现过的数字、日期和店铺名，并原样照抄（含 $、%、逗号和小数位）；
不得换算、估算或改写，不得出现任何新的数字、日期或店铺；CPC 出价建议只能引用 facts 中已给出的“a → b”。
口吻直接、口语化：先说动作，再说理由。不写开场白和总结。
headline：一句话点名最严重的问题（全店零曝光优先，其次花费最大的零出单或 ACOS 恶化），并说明为什么严重，不超过120字。
actions：3 到 5 条，每条只针对一个店铺，store 必须与 facts 中的店铺名完全一致；没有具体店铺时写“全局”。
只输出 json：{"headline":"…","actions":[{"store":"店铺名","text":"不超过160字"}]}"""


def _canonical(text):
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value


def _variants(value):
    exponent = -value.as_tuple().exponent if value.as_tuple().exponent < 0 else 0
    found = {value.normalize()}
    for digits in range(exponent):
        found.add(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP).normalize())
    return found


def _strip_dates(text):
    dates = set()

    def keep(match):
        month, day_ = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
        dates.add((int(month), int(day_)))
        return " "

    return _DATE.sub(keep, text), dates


def allowed_figures(texts):
    numbers, dates = set(), set()
    for text in texts:
        stripped, found = _strip_dates(str(text))
        dates |= found
        for match in _NUMBER.finditer(stripped):
            value = _canonical(match.group(1))
            if value is not None:
                numbers |= _variants(value)
    return numbers, dates


def unsupported_figures(text, allowed):
    numbers, dates = allowed
    stripped, found = _strip_dates(text)
    bad = [f"{month}-{day_}" for month, day_ in sorted(found - dates)]
    for match in _NUMBER.finditer(stripped):
        value = _canonical(match.group(1))
        if value is None or value.normalize() not in numbers:
            bad.append(match.group(0).strip())
    return bad


def _texts(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _texts(item)
    elif value is not None:
        yield str(value)


def _clean(text, limit):
    if not isinstance(text, str):
        raise ValueError
    text = " ".join(text.replace("\r", " ").split())
    if not text or len(text) > limit or _FORBIDDEN.search(text) or not _CJK.search(text):
        raise ValueError
    return text


def _check_daily(value, allowed):
    notes = value.get("notes") if isinstance(value, dict) else None
    if not isinstance(notes, list) or not 1 <= len(notes) <= 4:
        raise ValueError
    result, bad = [], []
    for note in notes:
        if not isinstance(note, dict):
            raise ValueError
        title, text = _clean(note.get("title"), 24), _clean(note.get("text"), 220)
        bad += unsupported_figures(title + " " + text, allowed)
        result.append({"title": title, "text": text})
    return result, bad


def _check_review(value, allowed, stores):
    headline = _clean(value.get("headline"), 160) if isinstance(value, dict) else None
    actions = value.get("actions") if isinstance(value, dict) else None
    if not isinstance(actions, list) or not 1 <= len(actions) <= 6:
        raise ValueError
    bad = unsupported_figures(headline, allowed)
    result = []
    for action in actions:
        if not isinstance(action, dict) or action.get("store") not in stores | {"全局"}:
            raise ValueError
        text = _clean(action.get("text"), 200)
        bad += unsupported_figures(text, allowed)
        result.append({"store": action["store"], "text": text})
    return {"headline": headline, "actions": result}, bad


def _ask(settings, system, packet, check, client_factory):
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError

    factory = client_factory or DeepSeekClient.from_settings
    try:
        client = factory(settings)
    except DeepSeekError:
        return None, "unavailable"
    allowed = allowed_figures(_texts(packet))
    user = json.dumps(packet, ensure_ascii=False)
    feedback = ""
    for _ in range(2):
        try:
            value = client.complete_json(system, user + feedback, max_tokens=1200)
            result, bad = check(value, allowed)
        except DeepSeekError:
            return None, "unavailable"
        except (ValueError, TypeError, AttributeError):
            feedback = "\n上次输出不符合 json 格式或字数要求，请严格按格式重写。"
            continue
        if not bad:
            return result, "deepseek"
        feedback = ("\n上次输出包含数据中没有的数字或日期：" + "、".join(dict.fromkeys(bad))[:200]
                    + "。只能原样使用 facts 中的数字，请重写。")
    _LOG.info("DeepSeek narrative rejected after verification; rule-based text used")
    return None, "rejected"


def _store_rows(report, limit=12):
    from jx_alert.sparkx.analysis import money, percent, signed

    cur = report["currency"]
    rows = []
    for site in report["sites"]:
        for store in site["stores"][:limit]:
            rows.append({"店铺": store["name"], "运营": store["operator"], "花费": money(store["current"]["spend"], cur),
                         "销售额": money(store["current"]["sales"], cur), "ACOS": percent(store["kpi"]["acos"]),
                         "花费日环比": signed(store["spend_change"], 0)})
    return rows


def daily_notes(settings, report, *, client_factory=None):
    """(notes, source): notes are [{"title", "text"}]; source deepseek|rules|unavailable|rejected."""
    rules = [{"title": "", "text": text} for text in report["notes"]]
    packet = {"report_date": report["report_date"], "previous_date": report["previous_date"],
              "currency": report["currency"], "facts": [fact["text"] for fact in report["facts"]],
              "tables": {"stores": _store_rows(report)},
              "rules": ["销售额、订单为截至生成时的归因数据，近 7–14 天仍会回补，花费与点击基本已定",
                        "高 ACOS 与零出单只统计花费 ≥ $50 的店铺"]}
    result, source = _ask(settings, DAILY_PROMPT, packet, _check_daily, client_factory)
    return (result, source) if result else (rules, "rules" if source == "deepseek" else source)


def review_text(settings, report, *, client_factory=None):
    """({"headline", "actions"}, source) with rule-based fallback."""
    stores = {row["name"] for row in report["overview"] + report["zero_stores"]}
    fallback = {"headline": None, "actions": report["actions"]}
    if not report["findings"] and not report["zero_stores"]:
        return fallback, "rules"
    packet = {"window": report["window"], "previous_window": report["previous_window"],
              "currency": report["currency"], "facts": [fact["text"] for fact in report["facts"]],
              "stores": sorted(stores)}
    result, source = _ask(settings, REVIEW_PROMPT, packet,
                          lambda value, allowed: _check_review(value, allowed, stores), client_factory)
    return (result, source) if result else (fallback, "rules" if source == "deepseek" else source)
