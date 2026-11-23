"""DeepSeek answers Feishu questions by calling read-only SparkX tools directly.

Tools exposed to the model: list_stores, query_ads_performance (get_ads_perf)
and query_entities (get_entity_metadata), each validated against the
monitored profiles. Every number in the final answer must appear in a tool
result; otherwise the model is asked once to correct itself, and then the raw
SparkX result is shown instead of unverified prose.
"""
from datetime import date, datetime, timedelta, timezone
import json
import logging

import anyio

from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.ai.narrative import allowed_figures, unsupported_figures
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import (
    QueryRejected, SparkXError, authorized_profiles, call, first_page, num, open_session, plain_key,
    validate_metadata_query, validate_performance_query,
)
from jx_alert.sparkx.collect import MARKETPLACE

_LOG = logging.getLogger(__name__)
MAX_ROUNDS = 7
MAX_TOOL_CALLS = 8
MAX_ROWS_TO_MODEL = 60
BASE = ("Impressions", "Clicks", "Spend", "Sales", "Conversions", "Units")

TOOLS = [
    {"type": "function", "function": {
        "name": "list_stores",
        "description": "列出已监控的 SparkX 店铺（profile_id、店铺名、站点、运营）以及美西今天与最近完整日。回答任何店铺问题前先调用。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "query_ads_performance",
        "description": ("只读查询 SparkX get_ads_perf 广告效果。fact_entity 决定粒度；dimensions 为分组字段，格式如 "
                        "campaign.campaignName_、campaign.campaignId_、profile.profileName_、date、adGroup.adGroupName_、"
                        "productAd.asin_、query_（searchTerm）；不传 dimensions 则返回总计。多店分开看时加 profile.profileName_。"
                        "指标名区分大小写：Impressions、Clicks、Spend、Sales、Conversions(=订单)、Units、ACOS、ROAS、CVR、CPC、CTR；"
                        "店铺总销售额/TACOS 只在 fact_entity=asin 时可用：TotalSalesAmount、OrderCount、TACOS。"
                        "百分比指标返回值已乘 100。需要环比时 compare_previous=true（自动查询前一个等长区间并给出 change）。"),
        "parameters": {"type": "object", "properties": {
            "profile_ids": {"type": "array", "items": {"type": "integer"},
                            "description": "店铺 profile_id，来自 list_stores；不传表示全部已监控店铺"},
            "fact_entity": {"type": "string", "enum": ["campaign", "adGroup", "target", "searchTerm",
                                                        "placement", "productAd", "asin"]},
            "date_start": {"type": "string", "description": "YYYY-MM-DD，店铺当地（美西）日期"},
            "date_end": {"type": "string", "description": "YYYY-MM-DD；默认用最近完整日，今天的数据是截至目前的累计"},
            "metrics": {"type": "array", "items": {"type": "string"}},
            "dimensions": {"type": "array", "items": {"type": "string"}},
            "filters": {"type": "object", "description": "如 {\"campaign.campaignState_\": \"enabled\", \"Spend\": {\">\": 50}}"},
            "order_by": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string"}, "direction": {"type": "string", "enum": ["ASC", "DESC"]}}}},
            "limit": {"type": "integer", "description": "最多返回行数，1–100，默认 30"},
            "query_type": {"type": "string", "enum": ["keyword", "product", "auto"],
                           "description": "仅 target / searchTerm 使用"},
            "hourly": {"type": "boolean", "description": "小时级数据，仅 campaign，跨度 ≤7 天"},
            "compare_previous": {"type": "boolean"}},
            "required": ["fact_entity", "date_start", "date_end", "metrics"]}}},
    {"type": "function", "function": {
        "name": "query_entities",
        "description": ("只读查询 SparkX get_entity_metadata 实体配置：campaign（状态、预算、竞价策略）、adGroup、target（出价）、"
                        "productAd、portfolio。filters/select 用 camelCase 字段名，如 {\"campaignState\": \"enabled\"}。"),
        "parameters": {"type": "object", "properties": {
            "entity": {"type": "string", "enum": ["campaign", "adGroup", "target", "productAd", "portfolio"]},
            "profile_ids": {"type": "array", "items": {"type": "integer"}},
            "filters": {"type": "object"},
            "select": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"}},
            "required": ["entity", "profile_ids"]}}},
]

SYSTEM_PROMPT = """你是 JX 的亚马逊广告数据助手，所有数据只来自 SparkX MCP 的只读工具。
现在是北京时间 {beijing}；店铺报表时区为美西（America/Los_Angeles）：美西今天 {today}（尚未结束），最近完整日 {latest}。
规则：
1. 先调用 list_stores 找到店铺 profile_id（用户可能说简称、站点或运营姓名）；需要任何数字时必须调用查询工具，不得凭记忆或编造。
2. 回答中的每个数字都必须原样来自工具结果（rows、totals、previous、change 字段），不要自己加减乘除出新数字；需要环比就用 compare_previous=true。
3. 今天的数据是截至目前的累计，订单归因会延迟数小时；最近 7–14 天的销售和订单仍会回补。缺失的数据就说缺失，不要当成 0。
4. 金额币种以工具结果的 currency 为准（多店查询为 USD）。百分比指标已乘 100，直接加 %。
5. 你只能读数据，不能修改广告、预算或出价，也不要声称已经执行了任何操作。
6. 用简洁的中文回答：先给结论，再用 Markdown 表格列关键数据（不超过 15 行），最后最多 3 条可执行建议。不输出 JSON、代码或链接。
7. 问题与广告数据无关时，简短说明你能查什么（店铺花费/销售/ACOS/TACOS、活动排名、环比、小时数据、广告配置）。"""


def _ints(values):
    return [int(value) for value in values]


def _derived(row):
    """ACOS/ROAS/CVR/CPC/CTR from base metrics when SparkX did not return them."""
    def value(key):
        try:
            return num(row.get(key))
        except SparkXError:
            return None

    spend, sales, clicks, orders, impressions = (value(key) for key in ("Spend", "Sales", "Clicks", "Conversions",
                                                                         "Impressions"))
    extra = {}
    if spend is not None and sales:
        extra.setdefault("ACOS", round(spend / sales * 100, 2))
    if sales is not None and spend:
        extra.setdefault("ROAS", round(sales / spend, 2))
    if orders is not None and clicks:
        extra.setdefault("CVR", round(orders / clicks * 100, 2))
    if spend is not None and clicks:
        extra.setdefault("CPC", round(spend / clicks, 2))
    if clicks is not None and impressions:
        extra.setdefault("CTR", round(clicks / impressions * 100, 2))
    return {key: val for key, val in extra.items() if key not in row}


def _totals(rows):
    totals = {}
    for key in BASE + ("TotalSalesAmount", "OrderCount", "UnitCount"):
        values = []
        for row in rows:
            if key not in row:
                break
            try:
                parsed = num(row[key])
            except SparkXError:
                parsed = None
            if parsed is None:
                break
            values.append(parsed)
        else:
            if values:
                totals[key] = round(sum(values), 2)
    if totals:
        totals.update(_derived(totals))
        if "TotalSalesAmount" in totals and "Spend" in totals and totals["TotalSalesAmount"]:
            totals.setdefault("TACOS", round(totals["Spend"] / totals["TotalSalesAmount"] * 100, 2))
    return totals


def _dimension_key(row, dimensions):
    """Join periods on the selected dimensions only (never on dates or metric values)."""
    return json.dumps([row.get(key) for key in dimensions], ensure_ascii=False, default=str)


def _change(current, previous):
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)) or previous <= 0:
        return None
    return f"{(current - previous) / previous * 100:+.1f}%"


class Agent:
    """One question: a single SparkX session and a bounded DeepSeek tool loop."""

    def __init__(self, settings, token, directory, *, now, client):
        self.settings, self.token, self.directory, self.now, self.client = settings, token, directory, now, client
        self.today = now.astimezone(MARKETPLACE).date()
        self.calls = 0
        self.results = []
        self.monitored = {}

    async def _list_stores(self, session):
        names = directory_module.display_names(self.monitored)
        return {"stores": [{"profile_id": int(key), "name": value["name"], "short_name": names[key],
                            "country": value["country"], "store_type": value["store_type"],
                            "operators": [row["name"] for row in directory_module.operators(self.directory, key)]}
                           for key, value in sorted(self.monitored.items(), key=lambda kv: names[kv[0]])],
                "today_marketplace": self.today.isoformat(),
                "latest_complete_day": (self.today - timedelta(days=1)).isoformat(),
                "timezone": "America/Los_Angeles"}

    async def _performance(self, session, args):
        ids = args.get("profile_ids") or _ints(sorted(self.monitored, key=int))
        query = {"profileIds": ids, "factEntity": args.get("fact_entity"), "dateStart": args.get("date_start"),
                 "dateEnd": args.get("date_end"), "metrics": args.get("metrics") or list(BASE),
                 "userContext": "JX 飞书问答（只读）", "language": "zh"}
        for source, target in (("dimensions", "select"), ("filters", "filters"), ("order_by", "orderBy"),
                               ("query_type", "queryType")):
            if args.get(source):
                query[target] = args[source]
        if args.get("hourly"):
            query["timeGranularity"] = "hourly"
        limit = args.get("limit", 30)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise QueryRejected("limit 须为 1–100")
        validate_performance_query({**query, "pageSize": limit}, set(self.monitored))
        requested = [str(value) for value in ids]
        rows, currency, more = await first_page(session, "get_ads_perf", query, requested, page_size=limit)
        metrics = set(query["metrics"])
        dimensions = [plain_key(field.rsplit(" as ", 1)[-1]) for field in query.get("select", [])
                      if plain_key(field.rsplit(" as ", 1)[-1]) not in {"date", "hour", "week", "month"}]
        result = {"currency": currency, "row_count": len(rows), "date_start": query["dateStart"],
                  "date_end": query["dateEnd"], "rows": [{**row, **_derived(row)} for row in rows[:MAX_ROWS_TO_MODEL]],
                  "totals_of_returned_rows": _totals(rows)}
        if more:
            result["note"] = "还有更多行未返回；totals 只代表已返回的行，不是全部合计"
        if args.get("compare_previous"):
            start, end = date.fromisoformat(query["dateStart"]), date.fromisoformat(query["dateEnd"])
            span = (end - start).days + 1
            previous = {**query, "dateStart": (start - timedelta(days=span)).isoformat(),
                        "dateEnd": (start - timedelta(days=1)).isoformat()}
            validate_performance_query({**previous, "pageSize": limit}, set(self.monitored))
            before, _, _ = await first_page(session, "get_ads_perf", previous, requested, page_size=limit)
            index = {_dimension_key(row, dimensions): row for row in before}
            for row in result["rows"]:
                prior = index.get(_dimension_key(row, dimensions))
                if prior:
                    row["previous"] = {key: prior.get(key) for key in metrics if key in prior}
                    row["change"] = {key: _change(row.get(key), prior.get(key)) for key in metrics
                                     if _change(row.get(key), prior.get(key))}
            result["previous_period"] = [previous["dateStart"], previous["dateEnd"]]
            result["previous_totals"] = _totals(before)
            result["totals_change"] = {key: _change(value, result["previous_totals"].get(key))
                                       for key, value in result["totals_of_returned_rows"].items()
                                       if _change(value, result["previous_totals"].get(key))}
        return result

    async def _entities(self, session, args):
        limit = args.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise QueryRejected("limit 须为 1–100")
        query = {"entity": args.get("entity"), "profileIds": args.get("profile_ids"),
                 "userContext": "JX 飞书问答（只读）", "page": 1, "pageSize": limit}
        for key in ("filters", "select"):
            if args.get(key):
                query[key] = args[key]
        validate_metadata_query(query, set(self.monitored))
        view = await call(session, "get_entity_metadata", query)
        data, meta = view.get("data") or {}, view.get("meta") or {}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        return {"rows": [{plain_key(k): v for k, v in row.items()} for row in rows[:MAX_ROWS_TO_MODEL]
                         if isinstance(row, dict)],
                "total": meta.get("total") if type(meta.get("total")) is int else None,
                "has_more": bool(meta.get("hasNextPage")), "currency_note": "配置金额为各店本币"}

    async def _execute(self, session, call_):
        name, args = call_["name"], call_["arguments"]
        self.calls += 1
        if self.calls > MAX_TOOL_CALLS:
            return {"error": "本次查询次数已达上限，请基于已有结果回答"}
        try:
            if name == "list_stores":
                result = await self._list_stores(session)
            elif name == "query_ads_performance":
                result = await self._performance(session, args)
            elif name == "query_entities":
                result = await self._entities(session, args)
            else:
                return {"error": "未知工具"}
        except QueryRejected as exc:
            return {"error": "查询被拒绝：" + str(exc)}
        except SparkXError as exc:
            return {"error": exc.message, "code": exc.code}
        except (TypeError, ValueError, KeyError):
            return {"error": "查询参数无效"}
        self.results.append({"tool": name, "result": result})
        return result

    async def run(self, question):
        beijing = self.now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(
            beijing=beijing, today=self.today.isoformat(), latest=(self.today - timedelta(days=1)).isoformat())},
            {"role": "user", "content": question}]
        timeout = float(getattr(self.settings, "sparkx_timeout", 30.0))
        async with open_session(self.token, timeout) as session:
            roster = await authorized_profiles(session)
            self.monitored = {key: value for key, value in roster.items()
                              if directory_module.monitored(self.directory, key)}
            corrected = False
            for _ in range(MAX_ROUNDS):
                try:
                    reply = await anyio.to_thread.run_sync(
                        lambda: self.client.chat(messages, tools=TOOLS, max_tokens=1800))
                except DeepSeekError as exc:
                    return {"answer": None, "provider": "deepseek_unavailable", "error": str(exc),
                            "calls": self.calls, "verified": False, "results": self.results[-1:]}
                if reply["tool_calls"]:
                    messages.append(reply["message"])
                    for call_ in reply["tool_calls"]:
                        output = await self._execute(session, call_)
                        text = json.dumps(output, ensure_ascii=False, default=str)[:24_000]
                        messages.append({"role": "tool", "tool_call_id": call_["id"], "content": text})
                    continue
                answer = reply["content"]
                material = [json.dumps(item["result"], ensure_ascii=False, default=str) for item in self.results]
                bad = unsupported_figures(answer, allowed_figures([question, self.today.isoformat(),
                                                                    (self.today - timedelta(days=1)).isoformat(),
                                                                    "7–14 天", *material]))
                if not bad:
                    return {"answer": answer, "provider": "deepseek", "calls": self.calls, "verified": True}
                if corrected:
                    break
                corrected = True
                messages.append(reply["message"])
                messages.append({"role": "user", "content": (
                    "你的回答包含工具结果中没有的数字：" + "、".join(dict.fromkeys(bad))[:200]
                    + "。请只使用工具结果里原样出现的数字重写回答；需要环比请调用 compare_previous=true。")})
        return {"answer": None, "provider": "unverified", "calls": self.calls, "verified": False,
                "results": self.results[-1:]}


def answer_question(settings, question, *, now=None, client_factory=None, token=None, directory=None,
                    total_timeout=150.0):
    """{"answer"|None, "provider", "calls", "results"?}; raises DeepSeekError/SparkXError codes."""
    from jx_alert.ai.deepseek import DeepSeekClient
    from jx_alert.sparkx.client import run
    from jx_alert.sparkx.credentials import effective_token

    now = now or datetime.now(timezone.utc)
    token = effective_token(settings) if token is None else token
    if not token:
        raise SparkXError("SPARKX_NOT_CONFIGURED", 409)
    client = (client_factory or DeepSeekClient.from_settings)(settings)
    directory = directory_module.load(settings) if directory is None else directory
    agent = Agent(settings, token, directory, now=now, client=client)
    return run(lambda: agent.run(question), total_timeout=total_timeout)
