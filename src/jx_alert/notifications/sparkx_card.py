"""Render one get_ads_perf result page, not a new report. No IO or model calls."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
import re

from mcp.types import CallToolResult

from jx_alert.sparkx_direct import SparkXDirectError, page_size_for


@dataclass(frozen=True)
class PagePreview:
    card: dict
    # The only payload allowed into an outbox. Free-form sibling MCP blocks never enter it.
    delivery_card: dict | None
    currency: str | None

    @property
    def eligible(self):
        """Whether this exact preview card can be enqueued."""
        return self.delivery_card is self.card


def _reject(message="SparkX 结果格式不支持，未生成可投递报告"):
    raise SparkXDirectError(message)


def _div(text, size="normal"):
    return {"tag": "div", "text": {"tag": "plain_text", "content": text, "text_size": size}}


def _text_block(text):
    # Use rich text ONLY for visibly formatted text made entirely of this
    # inert alphabet. No raw HTML, escapes, links, schemes, @, or autolinkable
    # domain names. Everything else remains literal, including code examples.
    alphabet = re.sub(r"(?<![\w.])[0-9]+\.[0-9]+(?![\w.])", "0", text)
    # Feishu's rich-text component supports four Markdown tables per block.
    rules = re.findall(r"(?m)^[ \t]*\|[-| ]{3,}\|[ \t]*$", text)
    if (("**" in text or text.startswith(("#", "- ")) or rules) and len(rules) <= 4
            and re.fullmatch(r"[\w \r\n*#%|+,，：；！？（）-]+", alphabet)):
        return {"tag": "markdown", "content": text}
    return _div(text)


def _display(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, (int, Decimal, str)):
        return str(value)
    _reject()


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        _reject()
    return value


def _parse(raw):
    try:
        return json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique,
                          parse_constant=lambda _: _reject())
    except (ValueError, RecursionError):
        _reject()


def _equal(left, right):
    # Python True == 1 and False == 0. A text/structured mirror must agree on
    # JSON types too; prefer the text mirror's decimal precision when equal.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _sources(result):
    if result.isError:
        _reject("SparkX 查询失败，未生成飞书报告")
    try:
        # Bound inputs in addition to the *double*-encoded outgoing message.
        encoded = result.model_dump_json(by_alias=True, exclude_none=True)
        if len(encoded.encode()) > 512_000 or len(result.content) > 32:
            _reject("SparkX 单页过大，请缩小 pageSize 或查询字段")
        envelope = (_parse(json.dumps(result.structuredContent, allow_nan=False))
                    if result.structuredContent is not None else None)
    except (ValueError, UnicodeError, RecursionError):
        _reject("SparkX 结果无法编码或过大")
    prose = []
    for block in result.content:
        audience = block.annotations.audience if block.annotations else None
        if block.type != "text" or (audience is not None and "user" not in audience):
            _reject()
        raw = block.text.strip()
        if not raw:
            continue
        if raw.startswith("```json"):
            if not raw.endswith("```"):
                _reject()
            raw = raw[len("```json"):-3].strip()
            if not raw.startswith("{"):
                _reject()
        if raw.startswith(("{", "[")) and not re.match(r"^\[[^\]\n]+\]\(", raw):
            parsed = _parse(raw)
            if not isinstance(parsed, dict) or (envelope is not None and not _equal(parsed, envelope)):
                _reject("SparkX 结构化结果未知或两种结果不一致")
            envelope = parsed
        else:
            prose.append(block.text)
    return envelope, prose


def _table(rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    if not keys or len(keys) > 50 or any(not k or len(k) > 80 for k in keys):
        _reject("SparkX 字段超出飞书表格限制，请减少或缩短字段")
    return {
        "tag": "table", "page_size": min(10, len(rows)), "row_height": "low",
        "freeze_first_column": True, "header_style": {"bold": True},
        "columns": [{"name": f"c{i}", "display_name": key, "data_type": "text"}
                    for i, key in enumerate(keys)],
        "rows": [{f"c{i}": _display(row[key]) if key in row else "（未返回）"
                  for i, key in enumerate(keys)} for row in rows],
    }


def _matches_profile(value, expected):
    return (type(value) is int or type(value) is str) and str(value) == str(expected)


def render_page(result: CallToolResult, query: dict) -> PagePreview:
    """The caller validated the exact request before calling get_ads_perf."""
    envelope, prose = _sources(result)
    profile = query["profileIds"][0]
    page = query.get("page", 1)
    page_size = page_size_for(query)
    audience = query["factEntity"] == "campaignAudience"
    status = f"请求第 {page} 页 · 供应端未确认分页状态，仅供预览"
    eligible, currency, notes, rows = False, None, [], []
    if envelope is not None:
        if "errorType" in envelope or envelope.get("isError", False) is not False:
            _reject("SparkX 查询失败，未生成飞书报告")
        if ("toolName" in envelope and envelope["toolName"] != "get_ads_perf"
                or envelope.keys() - {"isError", "toolName", "data", "meta", "requestId"}):
            _reject()
        data, meta = envelope.get("data"), envelope.get("meta", {})
        meta_keys = {"page", "pageSize", "hasNextPage", "effectiveProfileIds", "currency", "hint"}
        if audience:
            meta_keys.add("total")
        if (not isinstance(data, dict) or data.keys() - {"rows", "rowCount"}
                or not isinstance(meta, dict) or meta.keys() - meta_keys):
            _reject()
        rows = data.get("rows")
        if not isinstance(rows, list) or len(rows) > page_size:
            _reject()
        if "rowCount" in data and (type(data["rowCount"]) is not int or data["rowCount"] != len(rows)):
            _reject("SparkX 返回行数不一致")
        for key, expected in (("page", page), ("pageSize", page_size)):
            if key in meta and (type(meta[key]) is not int or meta[key] != expected):
                _reject("SparkX 返回分页与请求不符")
        has_next = meta.get("hasNextPage")
        if "hasNextPage" in meta and type(has_next) is not bool:
            _reject()
        total = meta.get("total")
        if "total" in meta:
            offset = (page - 1) * page_size
            if (type(total) is not int or total < 0 or len(rows) != max(0, min(page_size, total - offset))
                    or has_next is not None and has_next != (page * page_size < total)):
                _reject("SparkX 受众分页总数与本页不符")
        ids = meta.get("effectiveProfileIds")
        if "effectiveProfileIds" in meta and (
                not isinstance(ids, list) or len(ids) != 1 or not _matches_profile(ids[0], profile)):
            _reject("SparkX 实际账户与请求不符")
        for row in rows:
            if not isinstance(row, dict) or not row:
                _reject()
            for key, value in row.items():
                field = key.split(".")[-1].replace("_", "").casefold()
                if field == "profileid" and not _matches_profile(value, profile):
                    _reject("SparkX 结果行的账户与请求不符")
                if field in {"currency", "currencycode"} and "currency" in meta and value != meta["currency"]:
                    _reject("SparkX 结果行的币种与页面币种不符")
                if field == "date":
                    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                        _reject("SparkX 结果行的日期无法核实")
                    try:
                        day = date.fromisoformat(value).isoformat()
                    except ValueError:
                        _reject("SparkX 结果行的日期无法核实")
                    if not query["dateStart"] <= day <= query["dateEnd"]:
                        _reject("SparkX 结果行的日期超出请求范围")
                _display(value)
        if "currency" in meta:
            currency = meta["currency"]
            if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
                _reject("SparkX 返回币种无法确认")
        if "hint" in meta:
            if not isinstance(meta["hint"], str):
                _reject()
            notes.append(_div(meta["hint"], "notation"))
        if "requestId" in envelope:
            trace = envelope["requestId"]
            if not isinstance(trace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", trace):
                _reject()
            notes.append(_div("requestId: " + trace, "notation"))
        progress = {True: "还有下一页", False: "供应端标记本页为末页", None: "是否有下一页未确认"}[has_next]
        status = f"仅第 {page} 页，返回 {len(rows)} 行 · {progress} · 币种：{currency or '未返回'}"
        if total is not None:
            status += f" · 供应端总行数：{total}"
        eligible = bool(rows and (not audience or total is not None) and envelope.get("toolName") == "get_ads_perf"
                        and envelope.get("isError") is False and ids and currency and "rowCount" in data
                        and {"page", "pageSize", "hasNextPage"} <= meta.keys() and meta.get("hint", "").strip())
    if not prose and envelope is None:
        _reject("SparkX 没有返回可展示的文本或数据")
    header = _div(f"请求 profile {profile} · {query['factEntity']} · "
                  f"{query['dateStart']} 至 {query['dateEnd']} · {status}", "notation")

    def card(elements):
        value = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "SparkX 广告表现"}},
                 "body": {"elements": [header, *elements, *notes]}}
        # Reserve headroom for receive_id, uuid, and the enclosing IM API fields.
        packed = json.dumps(json.dumps(value, ensure_ascii=False), ensure_ascii=False).encode()
        if len(packed) > 18 * 1024:
            _reject("SparkX 单页超过飞书消息大小，请减少 pageSize 或字段；不会截断")
        return value

    # The vendor scopes the structured envelope, not neighbouring prose. A mixed result
    # may preview native text locally, but only the independently verified table can travel.
    scoped = card([_table(rows)]) if rows else None
    preview = (card([_text_block(text) for text in prose]) if prose else
               scoped or card([_div("本页没有返回记录；不表示业务值为零。")]))
    return PagePreview(card=preview, delivery_card=scoped if eligible else None, currency=currency)
