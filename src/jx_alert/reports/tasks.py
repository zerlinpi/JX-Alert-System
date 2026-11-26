"""What DeepSeek is asked to do for each lane, and how its answer is checked.

DeepSeek queries SparkX itself (owner decision, Nov 2026), as a SparkX MCP agent
with the official Skills. The owner can rewrite the task text of every lane in
the console; the scope (dates, stores) and the output contract around it are
fixed here. The daily report and the review come back as Markdown and are shown
as Feishu tables; the hourly monitor returns JSON rows so that each campaign and
category is alerted once per day and every row can be traced to a SparkX result.
"""
from datetime import timedelta
import json
import logging
import re

from jx_alert.ai import agent
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.reports import coverage, figures
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import SparkXError, run

_LOG = logging.getLogger(__name__)
CATEGORIES = {"high_spend_no_conversion": "高花费无转化", "high_spend_low_conversion": "高花费低转化",
              "low_spend_no_conversion": "低花费无转化", "low_spend_high_conversion": "低花费高转化"}
# Output budgets include the model's reasoning in thinking mode; a 7-day review of many stores needs room.
LIMITS = {"daily": {"calls": 40, "rounds": 30, "tokens": 64000, "timeout": 1500},
          "review": {"calls": 50, "rounds": 36, "tokens": 96000, "timeout": 2400},
          "hourly": {"calls": 24, "rounds": 20, "tokens": 48000, "timeout": 900}}
REQUEST_MINIMUM_TIMEOUT = 180
_STORES_LINE = re.compile(r"^\s*JX_STORES\s*[:：]\s*(.*)$", re.MULTILINE)

DEFAULT_PROMPTS = {
    "daily": """请按下面的口径和结构写日报。
口径：订单、销量、销售额都分「广告」和「总」两种，表格和正文都照下面的名称写：
- 广告花费、广告销售额、广告订单、广告销量、点击：factEntity=campaign 的 Spend、Sales、Conversions、Units、Clicks（含 SP、SB、SD 全部广告）。
- 总销售额、总订单、总销量（店铺全部销售，含自然单）：factEntity=asin 的 TotalSalesAmount、OrderCount、UnitCount。asin 表只取这三项，广告指标一律用 campaign 的合计。
- ACOS = 广告花费 ÷ 广告销售额；TACOS = 广告花费 ÷ 总销售额；CVR = 广告订单 ÷ 点击；CPC（PPC）= 广告花费 ÷ 点击；ROAS = 广告销售额 ÷ 广告花费；比率类的环比写百分点。
- 店铺总数据（总销售额、总订单、总销量）比广告数据更新得晚，也可能缺失：某店总订单为 0 但总销量大于 0，是总订单还没更新；总销售额为 0 但广告销售额大于 0，是 SparkX 没有返回这家店的店铺总数据。这两种情况写「—」，不当成 0。全公司的总销售额、总订单、总销量只加有数据的店铺；全公司 TACOS = 有总销售额数据的店铺的广告花费合计 ÷ 这些店铺的总销售额合计（不能用全公司广告花费去除）。
结构（按下面的顺序写，不加别的段落；第一行就是「全公司核心指标」，前面不写任何概述）：
1. **全公司核心指标**：表格「指标 | 当日 | 前一日 | 环比」，依次为总销售额、广告销售额、总订单、广告订单、总销量、广告销量、广告花费、ACOS、TACOS、CVR、PPC（CPC）、ROAS。
2. **站点汇总**（监控店铺分布在两个及以上站点时才写，只有一个站点时省略）：表格「站点 | 广告花费 | ACOS | TACOS | CPC | ROAS | CVR | 广告销售额 | 总销售额 | 广告订单 | 总订单 | 广告销量 | 总销量 | 花费环比」。
3. **有花费店铺明细**：表格「店铺 | 广告花费 | ACOS | TACOS | CPC | ROAS | CVR | 广告销售额 | 总销售额 | 广告订单 | 总订单 | 广告销量 | 总销量 | 花费日环比 | 运营」，按广告花费从高到低，只列当日有广告花费的店铺；表格下方不写说明或店铺名单。
4. **须知**：2–4 条编号短评，每条一两句话、不超过 60 字，先下结论再给关键数字：花费是否集中、ACOS 明显偏高的店铺、有广告花费但没有广告订单的店铺、整体效率的变化。没有发现的问题不写（不写「无」）；不写数据缺失、未更新、暂定值或币种之类的说明。
订单和销量写整数，百分比保留一位小数，CPC 写两位小数的金额，ROAS 保留两位小数。""",
    "review": """判定方法：先按 campaign 找出候选，再结合主推产品以往的数据和产品的价值、类型，判断是否真的异常、有多严重。
- 主推产品：只对候选 campaign 查（不扫全店）。用 factEntity=productAd、select campaign.campaignId_ 和 productAd.asin_，取每个候选 campaign 花费最多的 ASIN；再查这些 ASIN 在基准期（前 7 日之前的 30 天）的广告花费、广告销售额、广告订单、点击，以及售价 asin.asinPrice_、类目 asin.asinCategoryInfo_、SP 上线日 asin.asinOpenDate_。
- 产品价值：客单价 = 基准期广告销售额 ÷ 广告订单（基准期没有广告订单时用售价）。单笔允许花费 T = 客单价 × 目标 ACOS；没有给定目标 ACOS 时用该店近 30 天的 ACOS（Skill 规则）。
- 产品历史：基准期 CVR = 广告订单 ÷ 点击，ACOS = 广告花费 ÷ 广告销售额，CPA = 广告花费 ÷ 广告订单。基准期广告订单少于 3 单为样本不足：只按 T 判断，标注「样本不足」。
- 产品类型：SP 上线不足 90 天的新品、客单价 ≥ $150 的高客单产品转化慢、单量少，照常按 T 判断，但标注「新品期」「高客单」并把严重程度降一级；季节性明显的类目注明。
判定口径：
- 全店零曝光：两周内没有任何曝光，但仍有开启的 campaign（用 get_entity_metadata 统计开启数量），放在「最严重」。
- ① 高花费零出单：近 7 日广告订单 = 0，花费 ≥ T（已花掉一单允许的成本），且点击 ≥ 2 ÷ 基准期 CVR（按产品历史转化本应出 2 单以上；样本不足时不看这一条）；建议 CPC = 当前 CPC × 70%。
- ② ACOS 明显恶化（高花费低转化）：近 7 日花费 ≥ T，ACOS ≥ 产品基准期 ACOS 的 1.5 倍且至少高 15 个百分点（样本不足时与前 7 日比）；建议 CPC = 当前 CPC × 基准期 ACOS ÷ 近 7 日 ACOS，一次最多下调 50%（避免流量断崖）。
- ③ 加预算后销售反而下滑：近 7 日花费 ≥ T 且比前 7 日增加 ≥ 30%，广告销售额下降 ≥ 20%（或归零）。
- ④ 异常停摆：开启状态，前 7 日花费 ≥ T，近 7 日没有曝光。
- ⑤ 低花费高转化（机会，不计入异常）：近 7 日花费 < 3T、广告订单 ≥ 3，ACOS ≤ 产品基准期 ACOS 的 70% 且不高于店铺 ACOS。
- 托管组里的 campaign（campaign.aiGroupId_ 不为 0）由 AI 调价：只给托管组层面的建议（目标 ACOS、组预算），不给出价数字。
- 同类问题按浪费的金额排序：浪费 = 花费 − 广告订单 × 产品历史 CPA（没有历史时用 T）。
结构：
开头一句加粗：「K 个店铺检出 X 个异常 campaign，合计浪费约 $Y。」，紧接一句写近 7 日广告花费、广告销售额、ACOS 和它们较前 7 日的变化。开头不写店铺总数、有投放数据的店铺数、币种、暂定值和判定阈值的取法。
**一、最严重**：最需要马上处理的问题，一两句话说明为什么严重（带浪费金额和产品客单价）。
**二、异常店铺总览**：表格「店铺 | 近7日花费 | ACOS 近7日 | 前7日 | 异常数 | 主要问题 | 运营」。
**三、按问题类型看具体 campaign**：每类一张表，表前只写类型名称（例如「① 高花费零出单」），不重复判定条件；表格「店铺 | Campaign | 主推产品 | 客单价 | 近7日花费 | 点击 | 广告订单 | ACOS | 产品历史 | 建议 | 运营」：主推产品写 ASIN（可带简短品名），产品历史写基准期 CVR 和 ACOS（例如「CVR 6.2% / ACOS 28%」，样本不足写「样本不足」），建议写建议 CPC（例如 2.45 → 1.80；托管组写组层面建议）；⑤ 机会单独列出。没有检出的类型整段不写（不写「本期无检出」），也不写排查过程（例如「已确认处于暂停状态」）。
**四、建议动作**：3–5 条，每条针对上面已列出的一个异常店铺、不超过 60 字，先说动作再说理由，带具体的出价或预算建议（托管组除外）；没有列出的店铺不写。
口吻直接，像资深投手在群里复盘。""",
    "hourly": "",
}


def report_dates(now):
    """Latest completed marketplace day, the day before it and two 7-day windows (America/Los_Angeles)."""
    latest = now.astimezone(agent.MARKETPLACE).date() - timedelta(days=1)
    return {"report_date": latest, "previous_date": latest - timedelta(days=1),
            "window": (latest - timedelta(days=6), latest),
            "previous_window": (latest - timedelta(days=13), latest - timedelta(days=7))}


def marketplace_now(now):
    return now.astimezone(agent.MARKETPLACE)


def prompt_text(lane, config):
    return (config.get("prompt") or "").strip() or DEFAULT_PROMPTS[lane]


def _scope(monitored):
    ids = ", ".join(sorted(monitored, key=int))
    return (f"店铺范围：店铺目录中「纳入通知=是」的 {len(monitored)} 家店铺，profileIds = [{ids}]。"
            "对这些店铺做多店查询（金额由 SparkX 统一折算为 USD），不要包含其他店铺。")


_OUTPUT = ("输出要求：直接输出发到飞书群的正文（Markdown），不要开场白和结束语，不要代码块、链接或 @ 任何人；"
           "每句话都要有信息量，只写结论、数据和建议（所有者要求）：不写统计范围、监控或有数据的店铺数、币种、"
           "数据是暂定值、仍在累计或会回补、数据缺失或未更新之类的说明，不写「无」「无检出」这类没有内容的句子；"
           "不写数据来源、生成方式、查询次数和查询或分析过程，不复述口径和公式，"
           "不重复标题里已有的报告名称和日期；"
           "表格用标准 Markdown 表格；金额带 $ 符号，保留整数或两位小数；店铺写目录里的简称；有「运营」列的表格把「运营」放在最后一列，"
           "写目录里的运营（未指定写「未映射运营」）；订单、销量、销售额写明是广告的还是店铺总的"
           "（广告订单/总订单、广告销量/总销量、广告销售额/总销售额），不要只写「订单」「销量」「销售额」；"
           "缺失的数据写「—」，不要当成 0。")
# Which stores the hidden last line names, so the system can @ their operators.
_FLAGGED = {"daily": "「须知」里点名需要运营处理的店铺（例如 ACOS 明显偏高、有广告花费但没有广告订单；"
                     "只是店铺总数据缺失或未更新的不算）",
            "review": "检出异常（含全店零曝光）的店铺"}


def _stores_line(lane):
    return (f"最后单独一行写「JX_STORES: 」，后面跟{_FLAGGED[lane]}的 profileId，用英文逗号分隔；"
            "没有写「JX_STORES: 无」。这一行只给系统用来 @ 对应运营，不会展示。")


# The owner's task text is JX's own instruction: it ranks below SparkX's data and official Skills.
TASK_LABEL = "JX 任务要求（第二优先级：在不违背 SparkX 返回数据和官方 Skill 要求的前提下照做）："


def task_message(lane, config, *, now, monitored, alerted=()):
    """The user message of one scheduled run: fixed scope and contract + the owner's task text."""
    dates = report_dates(now)
    if lane == "daily":
        return ("【定时任务 · 08:30 广告日报】\n"
                f"统计日：{dates['report_date']}（美西已结束的完整日）；对比日：{dates['previous_date']}。\n"
                + _scope(monitored) + "\n" + _OUTPUT + "\n" + _stores_line(lane) + "\n\n" + TASK_LABEL + "\n"
                + prompt_text(lane, config))
    if lane == "review":
        w0, w1 = dates["window"]
        p0, p1 = dates["previous_window"]
        return ("【定时任务 · 08:30 广告异常复盘】\n"
                f"近 7 日：{w0} ~ {w1}；前 7 日：{p0} ~ {p1}（美西日期）。\n"
                + _scope(monitored) + "\n" + _OUTPUT + "\n" + _stores_line(lane) + "\n\n" + TASK_LABEL + "\n"
                + prompt_text(lane, config))
    local = marketplace_now(now)
    rules = (f"- high_spend_no_conversion 高花费无转化：花费 ≥ ${config['high_spend']:g} 且广告订单 = 0\n"
             f"- high_spend_low_conversion 高花费低转化：花费 ≥ ${config['high_spend']:g}，有广告订单，"
             f"但 ACOS ≥ {config['acos_high']:g}% 或 CVR < {config['cvr_low']:g}%\n"
             f"- low_spend_no_conversion 低花费无转化：${config['low_spend']:g} ≤ 花费 < ${config['high_spend']:g}，"
             f"点击 ≥ {config['min_clicks']:g}，广告订单 = 0\n"
             f"- low_spend_high_conversion 低花费高转化（放量机会）：花费 < ${config['high_spend']:g}，"
             f"广告订单 ≥ {config['min_orders_good']:g} 且 ACOS ≤ {config['acos_good']:g}%")
    seen = "、".join(sorted(alerted)[:300]) or "无"
    extra = prompt_text(lane, config)
    return ("【定时任务 · 每小时广告监控】\n"
            f"美西今天：{local:%Y-%m-%d}，当前美西时间 {local:%H:%M}（今日数据是截至目前的累计，订单归因会延迟数小时）。\n"
            + _scope(monitored) + "只看开启状态（enabled）的 campaign。\n"
            "提醒规则（金额 USD，广告订单 = Conversions）：\n" + rules + "\n"
            f"今天已经提醒过、本次不要再列出的（profileId:campaignId:类型）：{seen}\n"
            "可以用 get_ads_perf 的 filters 按花费、订单直接过滤，减少返回行数。\n"
            "最终回答只输出一个 JSON 对象，不要任何其他文字或代码块，格式："
            '{"marketplace_date":"YYYY-MM-DD","findings":[{"profile_id":123,"campaign_id":"456",'
            '"campaign_name":"…","category":"high_spend_no_conversion","spend":31.2,"clicks":20,"orders":0,'
            '"sales":0,"acos":null,"cvr":0,"suggestion":"不超过40字的处理建议"}],"note":"可选，一句话"}。'
            "campaign_id 必须照抄 SparkX 返回的值；没有需要提醒的活动时 findings 为空数组。"
            + (f"\n\n{TASK_LABEL}\n{extra}" if extra else ""))


# ---------------------------------------------------------------- answer checks

def split_stores(content, monitored):
    """(report text without the JX_STORES line, [profile ids it names] or None when absent)."""
    found = list(_STORES_LINE.finditer(content or ""))
    if not found:
        return (content or "").strip(), None
    ids = re.findall(r"[1-9][0-9]{0,19}", found[-1].group(1))
    text = _STORES_LINE.sub("", content).strip()
    return text, [value for value in dict.fromkeys(ids) if value in monitored]


def parse_json(content):
    text = (content or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _number(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        raise ValueError("bad number")
    return float(value)


def _consistent(category, row, config):
    spend, clicks, orders, acos, cvr = row["spend"], row["clicks"], row["orders"], row["acos"], row["cvr"]
    high, low = float(config["high_spend"]), float(config["low_spend"])
    if category == "high_spend_no_conversion":
        return spend >= high and orders == 0
    if category == "high_spend_low_conversion":
        return spend >= high and orders > 0 and ((acos is not None and acos >= config["acos_high"])
                                                  or (cvr is not None and cvr < config["cvr_low"]))
    if category == "low_spend_no_conversion":
        return low <= spend < high and clicks >= config["min_clicks"] and orders == 0
    return spend < high and orders >= config["min_orders_good"] and acos is not None and acos <= config["acos_good"]


def hourly_findings(value, summary, *, monitored, config, alerted):
    """Validated, new findings; rows that cannot be traced to SparkX or break the rules are dropped."""
    rows = value.get("findings")
    if not isinstance(rows, list):
        raise ValueError("findings must be a list")
    kept, dropped, keys = [], 0, set()
    for item in rows[:300]:
        try:
            pid = str(item["profile_id"])
            cid = str(item["campaign_id"]).strip()
            category = item["category"]
            if (pid not in monitored or category not in CATEGORIES or not re.fullmatch(r"[0-9A-Za-z_-]{1,40}", cid)
                    or not agent.evidence_contains(summary, cid)):
                raise ValueError("untraceable")
            row = {"pid": pid, "campaign_id": cid, "category": category,
                   "campaign": str(item.get("campaign_name") or cid)[:120],
                   "spend": _number(item.get("spend")), "clicks": _number(item.get("clicks")),
                   "orders": _number(item.get("orders")), "sales": _number(item.get("sales"), optional=True),
                   "acos": _number(item.get("acos"), optional=True), "cvr": _number(item.get("cvr"), optional=True),
                   "suggestion": str(item.get("suggestion") or "")[:80]}
            if not _consistent(category, row, config):
                raise ValueError("rule mismatch")
        except (KeyError, TypeError, ValueError, AttributeError):
            dropped += 1
            continue
        key = finding_key(row)
        if key in alerted or key in keys:
            continue
        keys.add(key)
        kept.append(row)
    return kept, dropped


def finding_key(row):
    return f"{row['pid']}:{row['campaign_id']}:{row['category']}"


# ---------------------------------------------------------------- running a lane

def _client(settings, client_factory):
    from jx_alert.ai.deepseek import DeepSeekClient

    return (client_factory or (lambda value: DeepSeekClient.from_settings(
        value, minimum_timeout=REQUEST_MINIMUM_TIMEOUT)))(settings)


def _strip(summary):
    return {key: value for key, value in summary.items() if key not in {"evidence", "requests"}}


def state_coverage(text, summary, *, monitored, roster, day, previous):
    """(the daily report as the system settles it, the stores that stopped spending; see coverage).

    DeepSeek's own notes under the store table and remarks without information in 须知 go; the stores
    that spent on ``previous`` and nothing on ``day`` are named from SparkX's rows.
    """
    found = coverage.stopped(summary, monitored=monitored, day=day, previous=previous)
    if found:
        _LOG.info("Daily report: %d store(s) spent the day before and nothing on the day", len(found))
    return coverage.apply(text, coverage.note(found, directory_module.display_names(roster))), sorted(found, key=int)


def settle_daily(text, summary, *, monitored, roster, dates):
    """(the daily report as sent, the stores the system adds to the flagged ones).

    The system's part of the daily report (owner, Nov 2026): no overview before the first section,
    every 环比 agrees with its row, the store table is led by the figures the owner reads first, and
    the statements about stores come from SparkX's rows.
    """
    text = coverage.without_preamble(text)
    text, corrected = figures.check_changes(text)
    if corrected:
        _LOG.info("Daily report: corrected %d 环比 figure(s) that did not match their own row", corrected)
    text = figures.arrange_store_table(text)
    return state_coverage(text, summary, monitored=monitored, roster=roster, day=dates["report_date"],
                          previous=dates["previous_date"])


def run_lane(settings, lane, config, *, now, directory, alerted=frozenset(), on_roster=None, token=None,
             client_factory=None):
    """One scheduled (or preview) run: DeepSeek queries SparkX and writes the lane's content.

    Raises SparkXError / DeepSeekError (safe codes and messages) or ValueError when the
    answer does not satisfy the output contract.
    """
    from jx_alert.sparkx.credentials import effective_token

    token = effective_token(settings) if token is None else token
    if not token:
        raise SparkXError("SPARKX_NOT_CONFIGURED", 409)
    client = _client(settings, client_factory)
    index = skill_store.for_agent(settings)  # installed Skills the owner left switched on
    limits = LIMITS[lane]
    state = {}

    def messages_for(context):
        roster = context["roster"]
        monitored = {key for key in roster if directory_module.monitored(directory, key)}
        if not monitored:
            raise SparkXError("SPARKX_NO_PROFILES", 409)
        state["monitored"] = monitored
        system = agent.system_prompt(now=now, tool_names=context["tool_names"], skills_index=context["skills"],
                                     roster=roster, directory=directory, instructions=context["instructions"],
                                     only_monitored=True)
        return [{"role": "system", "content": system},
                {"role": "user", "content": task_message(lane, config, now=now, monitored=monitored,
                                                         alerted=alerted)}]

    async def conversation():
        return await agent.run_task(settings, client, token=token, messages_for=messages_for, now=now,
                                    directory=directory, max_tool_calls=limits["calls"],
                                    max_rounds=limits["rounds"], max_tokens=limits["tokens"],
                                    on_roster=on_roster, skills_index=index)

    summary, context = run(conversation, total_timeout=limits["timeout"], passthrough=(DeepSeekError,))
    monitored = state["monitored"]
    result = {"summary": _strip(summary), "profiles": sorted(monitored, key=int), "roster": context["roster"],
              "skills": (index or {}).get("release")}
    if lane == "hourly":
        try:
            value = parse_json(summary["content"])
        except ValueError:
            value = _repair_json(client, summary["content"])
        findings, dropped = hourly_findings(value, summary, monitored=monitored, config=config, alerted=alerted)
        local = marketplace_now(now)
        result.update(findings=findings, dropped=dropped, note=str(value.get("note") or "")[:200],
                      day=local.date().isoformat(), time=f"{local:%H:%M}")
        if dropped:
            _LOG.info("Hourly monitor dropped %d untraceable or inconsistent finding(s)", dropped)
        return result
    text, stores = split_stores(summary["content"], monitored)
    dates = report_dates(now)
    stopped = []
    if lane == "daily" and text:
        text, stopped = settle_daily(text, summary, monitored=monitored, roster=context["roster"], dates=dates)
    if not text:
        raise ValueError("empty report")
    if stores is None:
        stores = affected_stores(client, text, context["roster"], monitored, lane=lane)
    # A store that stopped spending is flagged too: its operator is named (and @-mentioned) once, that day.
    stores = list(dict.fromkeys([*(stores or []), *stopped]))
    result.update(content=text, stores=stores, report_date=dates["report_date"].isoformat(),
                  window=[value.isoformat() for value in dates["window"]])
    return result


def _repair_json(client, content):
    """One no-tool request turning the agent's last answer into the required JSON object."""
    value = client.complete_json(
        "把下面这段每小时广告监控结果整理成 json 对象，字段：marketplace_date、findings（profile_id、campaign_id、"
        "campaign_name、category、spend、clicks、orders、sales、acos、cvr、suggestion）、note。只使用原文中的数据，"
        "不要增加原文没有的活动。", (content or "")[:60_000], max_tokens=8000)
    return value


_AFFECTED = {
    "daily": "根据广告日报正文，找出「须知」里点名需要运营处理的店铺（例如 ACOS 明显偏高、有花费没出单）",
    "review": "根据广告异常复盘正文，找出被判定为有异常（含全店零曝光）的店铺；机会类（低花费高转化）不算异常",
}


def affected_stores(client, text, roster, monitored, *, lane="review"):
    """Profile IDs the report names as needing an operator, when the agent omitted the JX_STORES line."""
    names = directory_module.display_names(roster)
    stores = [{"profile_id": key, "name": roster[key]["name"], "short_name": names[key]}
              for key in sorted(monitored, key=int)]
    try:
        value = client.complete_json(
            _AFFECTED[lane] + "，输出 json：{\"profile_ids\":[\"…\"]}。只能使用给出的店铺列表中的 profile_id。",
            json.dumps({"stores": stores, "report": text[:60_000]}, ensure_ascii=False), max_tokens=2000)
    except DeepSeekError:
        return []
    ids = value.get("profile_ids") if isinstance(value, dict) else None
    return [str(item) for item in ids if str(item) in monitored] if isinstance(ids, list) else []


def scope_report(settings, text, store_names, *, client_factory=None):
    """The review limited to one operator's stores (no tools, nothing added); None when nothing remains."""
    client = _client(settings, client_factory)
    result = client.chat([
        {"role": "system", "content": "你是编辑。把下面的广告异常复盘裁剪成只与指定店铺有关的版本：保留原有结构、"
                                      "表格和数字，删除其他店铺的行和段落，不增加任何内容。如果没有与这些店铺有关的内容，只输出 NONE。"},
        {"role": "user", "content": "指定店铺：" + "、".join(store_names) + "\n\n复盘正文：\n" + text[:60_000]}],
        max_tokens=16000)
    scoped = result["content"].strip()
    return None if not scoped or scoped.upper() == "NONE" else scoped
