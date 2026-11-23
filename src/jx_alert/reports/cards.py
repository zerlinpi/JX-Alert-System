"""Feishu cards for the SparkX lanes: 08:30 daily report, 7-day review, hourly monitor.

Store and campaign names are untrusted provider text: they appear in plain-text
table cells, or in markdown only after md_safe. Narrative text (DeepSeek or
rule-based) is md_safe too. A missing value renders as "—", never as 0.
"""
from jx_alert.notifications.cards import card_fits, markdown, md_safe, shell, table
from jx_alert.sparkx.analysis import (
    HOURLY_LABELS, HOURLY_ORDER, REVIEW_LABELS, amount, change, count, money, percent, ratio, signed, times,
)

_NUMERALS = "一二三四五六七八九十"
_COUNTS = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}
SOURCES = {"deepseek": "说明由 DeepSeek 依据上表数据撰写，数字已逐项核对。",
           "rules": "说明由系统规则生成。",
           "unavailable": "DeepSeek 暂不可用，说明由系统规则生成。",
           "rejected": "DeepSeek 文字未通过数字核对，已改用系统规则说明。"}
ATTRIBUTION = "销售额、订单为 SparkX 截至生成时的归因数据，近 7–14 天仍会回补；花费与点击基本已定。"


def _pp(current, previous):
    if current is None or previous is None:
        return "—"
    return f"{(current - previous) * 100:+.1f}pp"


def _ratio_change(current, previous):
    return signed(change(current, previous))


# ---------------------------------------------------------------- daily report

def _kpi_rows(company, cur):
    now, before = company["current"], company["previous"]
    rows = []
    if company["business_available"]:
        rows += [("店铺总销售额", money(now["total_sales"], cur), money(before["total_sales"], cur),
                  _ratio_change(now["total_sales"], before["total_sales"])),
                 ("总订单量", count(now["total_orders"]), count(before["total_orders"]),
                  _ratio_change(now["total_orders"], before["total_orders"]))]
    rows += [("广告花费", money(now["spend"], cur), money(before["spend"], cur), _ratio_change(now["spend"], before["spend"])),
             ("广告销售额", money(now["sales"], cur), money(before["sales"], cur),
              _ratio_change(now["sales"], before["sales"])),
             ("广告订单", count(now["orders"]), count(before["orders"]), _ratio_change(now["orders"], before["orders"])),
             ("ACOS", percent(now["acos"]), percent(before["acos"]), _pp(now["acos"], before["acos"]))]
    if company["business_available"]:
        rows.append(("TACOS", percent(now["tacos"]), percent(before["tacos"]), _pp(now["tacos"], before["tacos"])))
    rows += [("CVR（订单/点击）", percent(now["cvr"]), percent(before["cvr"]), _pp(now["cvr"], before["cvr"])),
             ("PPC（花费/点击）", money(now["cpc"], cur, cents=True), money(before["cpc"], cur, cents=True),
              _ratio_change(now["cpc"], before["cpc"])),
             ("ROAS", times(now["roas"]), times(before["roas"]), _ratio_change(now["roas"], before["roas"]))]
    return rows


def _site_row(label, cur, stores, with_data, site):
    now = site["current"]
    return (label, cur or "—", str(stores), str(with_data), amount(now["spend"], 0), amount(now["sales"], 0),
            count(now["units"]), count(now["orders"]), percent(now["acos"]), times(now["roas"]),
            signed(site["spend_change"]), signed(site["sales_change"]))


def _store_row(store, *, site=None):
    now = store["current"]
    spend_change = ("前日为 0" if store["spend_change"] is None and (store["previous"] or {}).get("spend") == 0
                    else "前日无记录" if store["previous"] is None else signed(store["spend_change"], 0))
    row = (store["name"], store["operator"], amount(now["spend"]), amount(now["sales"]), count(now["units"]),
           count(now["orders"]), percent(store["kpi"]["acos"]), percent(store["kpi"]["tacos"]), spend_change)
    return ((site,) + row) if site else row


def _numbered(notes, *, titled):
    lines = []
    for index, note in enumerate(notes, 1):
        title = md_safe(note.get("title"), 24) if titled and note.get("title") else ""
        lines.append(f"{index}. " + (f"**{title}**：" if title else "") + md_safe(note["text"], 600))
    return "\n".join(lines)


def _daily_elements(report, notes, source, cap):
    cur, company = report["currency"], report["company"]
    day, before = report["report_date"], report["previous_date"]
    scope = (f"统计日 {day}（美西时间，已结束的完整日），环比 {before}。监控店铺 {company['store_count']} 个，"
             f"SparkX 有数据 {company['with_rows']} 个，当日有花费 {company['with_spend']} 个。"
             f"金额单位 {cur or '—'}" + ("（多店由 SparkX 统一折算）。" if cur == "USD" and company["store_count"] > 1
                                       else "。"))
    elements = [markdown(scope), markdown("**全公司核心指标**"),
                table(["指标", "当日", "前一日", "环比"], _kpi_rows(company, cur))]
    coverage = []
    if company["previous_rows"] != company["with_rows"]:
        coverage.append(f"广告数据当日 {company['with_rows']} 家、前一日 {company['previous_rows']} 家")
    if company["business_available"] and company["previous_business_stores"] != company["business_stores"]:
        coverage.append(f"店铺总销售当日 {company['business_stores']} 家、前一日 {company['previous_business_stores']} 家")
    if coverage:
        elements.append(markdown("环比口径：" + "；".join(coverage) + "有数据，按各自合计计算。"))
    site_rows = [_site_row(site["label"], cur, site["store_count"], site["with_data"], site)
                 for site in report["sites"]]
    if len(report["sites"]) > 1:
        site_rows.append(_site_row(f"合计（{cur or '—'}）", cur, company["store_count"], company["with_spend"],
                                   {"current": company["current"],
                                    "spend_change": change(company["current"]["spend"], company["previous"]["spend"]),
                                    "sales_change": change(company["current"]["sales"], company["previous"]["sales"])}))
    elements += [markdown("**站点汇总**"), table(["站点", "币种", "店铺数", "有数据", "花费", "销售额", "销量", "订单",
                                              "ACOS", "ROAS", "花费环比", "销售额环比"], site_rows)]
    spending = [site for site in report["sites"] if site["stores"]]
    headers = ["店铺", "运营", "花费", "销售额", "销量", "订单", "ACOS", "TACOS", "花费日环比"]
    if len(spending) == 1:
        site = spending[0]
        rows = [_store_row(store) for store in site["stores"][:cap]]
        elements += [markdown(f"**{site['label']} {len(site['stores'])} 个有花费店铺明细**"), table(headers, rows)]
        hidden = len(site["stores"]) - len(rows)
    elif spending:
        stores = [(site["label"], store) for site in spending for store in site["stores"]]
        rows = [_store_row(store, site=label) for label, store in stores[:cap]]
        elements += [markdown(f"**{len(stores)} 个有花费店铺明细**"), table(["站点", *headers], rows)]
        hidden = len(stores) - len(rows)
    else:
        hidden = 0
        elements.append(markdown("当日没有任何店铺产生广告花费。"))
    if hidden > 0:
        elements.append(markdown(f"另有 {hidden} 个有花费店铺未展开，可在控制台预览完整明细。"))
    for site in report["sites"]:
        if site["quiet"]:
            elements.append(markdown(f"{site['label']}当日无花费：" + "、".join(md_safe(n, 30) for n in site["quiet"][:30])))
        if site["no_rows"] and not site["empty"]:
            elements.append(markdown(f"{site['label']} SparkX 无记录：" + "、".join(md_safe(n, 30)
                                                                                for n in site["no_rows"][:30])))
    titled = source == "deepseek"
    label = _COUNTS.get(len(notes), str(len(notes)))
    elements += [{"tag": "hr"}, markdown(f"**{label}点须知**\n" + _numbered(notes, titled=titled)),
                 markdown(f"数据来源：SparkX MCP 只读直出。{ATTRIBUTION}{SOURCES.get(source, '')}")]
    return elements


def daily_card(report, notes, source):
    for cap in (25, 15, 10, 6):
        value = shell(f"SparkX 广告日报 · {report['report_date']}", _daily_elements(report, notes, source, cap))
        if card_fits(value):
            return value
    return None


# ---------------------------------------------------------------- 7-day review

def _site_summary(site, cur):
    w, p = site["w"] or {}, site["p"] or {}
    text = (f"{site['label']}合计：花费 {money(w.get('spend'), cur)}（前 7 日 {money(p.get('spend'), cur)}，"
            f"{signed(change(w.get('spend'), p.get('spend')))}），销售额 {money(w.get('sales'), cur)}"
            f"（{signed(change(w.get('sales'), p.get('sales')))}），ACOS {percent(site['acos_p'])} → "
            f"{percent(site['acos_w'])}")
    sales = change(w.get("sales"), p.get("sales"))
    if site["acos_w"] is not None and site["acos_p"] is not None and sales is not None:
        text += ("——整体没有明显问题，问题集中在个别店铺和个别 campaign。"
                 if site["acos_w"] - site["acos_p"] <= 0.03 and sales >= -0.10 else "——整体效率在下滑，需要重点关注。")
    return text


def _category_rows(code, items, cur):
    rows = []
    for item in items:
        w, p, advice = item["w"] or {}, item["p"] or {}, item["advice"]
        head = (item["store"], item["operator"], item["campaign"])
        bid = (f"{advice['cpc']:.2f} → {advice['target']:.2f}" if advice.get("target") is not None else "—")
        if code == "zero_orders":
            rows.append((*head, money(w.get("spend"), cur), count(w.get("clicks")), bid))
        elif code == "acos_worse":
            rows.append((*head, percent(ratio(p.get("spend"), p.get("sales")), 0) + " → "
                         + percent(ratio(w.get("spend"), w.get("sales")), 0),
                         money(w.get("spend"), cur), money(w.get("sales"), cur), bid))
        elif code == "surge":
            sales = "归零" if w.get("sales") == 0 else signed(change(w.get("sales"), p.get("sales")), 0)
            rows.append((*head, f"{signed(change(w.get('spend'), p.get('spend')), 0)}（{money(w.get('spend'), cur)}）",
                         sales, money(p.get("sales"), cur) + " → " + money(w.get("sales"), cur)))
        else:
            seen = "无投放记录" if not item["w"] else count(w.get("impressions")) + " 曝光"
            rows.append((*head, money(p.get("spend"), cur), seen))
    return rows


_CATEGORY_TABLES = (
    ("zero_orders", "① 高花费零出单（钱花了，一单没有）",
     ["店铺", "运营", "Campaign", "近7日花费", "点击", "建议 CPC"]),
    ("acos_worse", "② ACOS 明显恶化（效率下降）",
     ["店铺", "运营", "Campaign", "ACOS 前7日→近7日", "近7日花费", "近7日销售", "建议 CPC"]),
    ("surge", "③ 加预算后销售反而下滑", ["店铺", "运营", "Campaign", "花费变化", "销售变化", "销售额"]),
    ("stalled", "④ 异常停摆（开启但完全不消耗）", ["店铺", "运营", "Campaign", "前7日花费", "近7日"]),
)


def _review_elements(report, text, source, cap, store_cap):
    cur = report["currency"]
    w0, w1 = report["window"]
    p0, p1 = report["previous_window"]
    findings = report["findings"]
    scoped = report.get("scoped", False)
    sites = [site for site in report["sites"] if site["store_count"]]
    label = sites[0]["label"] if len(sites) == 1 else ""
    if scoped:
        lead = (f"**你负责的店铺中，{report['affected']} 个检出 {len(findings)} 个异常 campaign。**"
                if findings else "**你负责的店铺中没有检出异常 campaign。**")
    else:
        lead = (f"**{report['store_total']} 个{label}店铺中，{report['active_total']} 个有投放数据，"
                f"{report['affected']} 个检出 {len(findings)} 个异常 campaign。**")
    scope = f"统计范围：{w0}~{w1}（近 7 日）对比 {p0}~{p1}（前 7 日），campaign 级。"
    if not scoped:
        scope += "\n" + "\n".join(_site_summary(site, cur) for site in sites if site["w"])
    elements = [markdown(lead + "\n" + scope)]
    sections = []
    serious = []
    if text.get("headline"):
        serious.append(md_safe(text["headline"], 300))
    for row in report["zero_stores"][:3]:
        serious.append(f"**{md_safe(row['name'], 40)} 全店零曝光**（运营：{md_safe(row['operator'], 40)}）："
                       f"{row['enabled']} 个 campaign 处于开启状态，但近两周 SparkX 没有返回任何曝光、花费或销售记录。"
                       "这不是某个广告表现差，而是整店广告没有投放。常见原因：账户余额或信用卡失效、店铺被暂停、"
                       "Listing 全部下架、预算被清零。")
    if not serious and findings:
        top = max(findings, key=lambda item: ((item["w"] or item["p"] or {}).get("spend") or 0))
        serious.append(f"花费最大的异常：{md_safe(top['store'], 40)} 的 {md_safe(top['campaign'], 60)}"
                       f"（{REVIEW_LABELS[top['category']]}）。")
    if serious:
        sections.append(("最严重", [markdown("\n\n".join(serious))]))
    if report["overview"]:
        rows = [(row["name"], row["operator"], money((row["w"] or {}).get("spend"), cur),
                 percent(row["acos_w"], 0), percent(row["acos_p"], 0), str(row["count"]), row["problem"])
                for row in report["overview"][:store_cap]]
        body = [table(["店铺", "运营", "近7日花费", "ACOS 近7日", "前7日", "异常数", "主要问题"], rows)]
        if len(report["overview"]) > store_cap:
            body.append(markdown(f"另有 {len(report['overview']) - store_cap} 个店铺未展开。"))
        sections.append(("异常店铺总览", body))
    category = []
    for code, heading, headers in _CATEGORY_TABLES:
        items = report["grouped"][code]
        if not items:
            continue
        category += [markdown(f"**{heading}**"), table(headers, _category_rows(code, items[:cap], cur))]
        if len(items) > cap:
            category.append(markdown(f"另有 {len(items) - cap} 个{REVIEW_LABELS[code]}活动未展开。"))
    if category:
        sections.append(("按问题类型看具体 campaign", category))
    actions = text.get("actions") or []
    if actions:
        sections.append(("建议动作", [markdown("\n".join(
            f"{index}. " + (f"**{md_safe(row['store'], 30)}**：" if source == "deepseek" and row.get("store")
                            and row["store"] != "全局" else "") + md_safe(row["text"], 400)
            for index, row in enumerate(actions, 1)))]))
    for index, (heading, body) in enumerate(sections):
        elements.append(markdown(f"**{_NUMERALS[index]}、{heading}**"))
        elements.extend(body)
    if report["idle_stores"] and not scoped:
        names = "、".join(md_safe(row["name"], 30) for row in report["idle_stores"][:20])
        reason = ("未取得活动配置，无法区分是没开广告还是授权范围不含广告" if report["metadata_unavailable"]
                  else "查不到开启的 campaign，更像是没有开广告或授权范围不含广告")
        elements.append(markdown(f"另有 {len(report['idle_stores'])} 个店铺两周零数据（{names}），{reason}，暂不计为异常。"))
    if report["missing"] and not scoped:
        elements.append(markdown(f"{len(report['missing'])} 个已开启监控的店铺不在当前 Token 授权内，已跳过。"))
    elements += [{"tag": "hr"}, markdown(
        "数据：SparkX get_ads_perf 直出（campaign 级），近 7 日的订单和销售仍会回补。建议 CPC = 当前平均 CPC × 目标 ACOS ÷ "
        "当前 ACOS（零出单按先降 30% 起步）。本报告只读，不会修改任何广告、预算或出价；调整请运营确认后在广告后台操作。"
        + SOURCES.get(source, ""))]
    return elements


def review_card(report, text, source):
    if report.get("scoped") and not report["findings"] and not report["zero_stores"]:
        return None
    color = "orange" if report["findings"] or report["zero_stores"] else "green"
    for cap, store_cap in ((10, 15), (6, 10), (4, 6), (2, 4)):
        value = shell("SparkX 广告异常复盘 · 近 7 日对比前 7 日",
                      _review_elements(report, text, source, cap, store_cap), color)
        if card_fits(value):
            return value
    return None


# ---------------------------------------------------------------- hourly monitor

_HOURLY_TIPS = {
    "high_no_conv": "高花费无转化：先看今日搜索词，否定无关词；若是新开或测试活动，可下调出价或暂停观察。",
    "high_low_conv": "高花费低转化：复核出价与广告位溢价，检查 Listing 价格、评分与库存。",
    "low_no_conv": "低花费无转化：小额漏损，持续观察；连续多日如此再降价或否词。",
    "low_high_conv": "低花费高转化：效果好但花得少，可适当提高预算或出价放量。",
}


def hourly_card(report, *, cap=8):
    new = report["new"]
    if not new:
        return None
    cur, t = report["currency"], report["thresholds"]
    stores = len({item["pid"] for item in new})
    scope = ("你负责的店铺中，" if report.get("scoped") else "") + f"{stores} 个店铺 {len(new)} 个广告活动触发提醒。"
    elements = [markdown(f"美西 {report['day']} 今日截至 {report.get('marketplace_time') or '—'}"
                         f"（SparkX 当日累计，订单归因会延迟数小时）：{scope}")]
    headers = ["店铺", "运营", "广告活动", "花费", "点击", "订单", "ACOS", "CVR", "PPC"]
    for code in HOURLY_ORDER:
        items = report["grouped"][code]
        if not items:
            continue
        rows = [(item["store"], item["operator"], item["campaign"], money(item["t"]["spend"], cur, cents=True),
                 count(item["t"]["clicks"]), count(item["t"]["orders"]), percent(item["kpi"]["acos"]),
                 percent(item["kpi"]["cvr"]), money(item["kpi"]["cpc"], cur, cents=True)) for item in items[:cap]]
        elements += [markdown(f"**{HOURLY_LABELS[code]}（{len(items)}）**"), table(headers, rows)]
        if len(items) > cap:
            elements.append(markdown(f"另有 {len(items) - cap} 个{HOURLY_LABELS[code]}活动未展开。"))
    tips = [_HOURLY_TIPS[code] for code in HOURLY_ORDER if report["grouped"][code]]
    rule = (f"判定：高花费 ≥ ${t['high_spend']:g}；低转化 = ACOS ≥ {t['acos_high']:g}% 或 CVR < {t['cvr_low']:g}%；"
            f"低花费无转化 = 花费 ≥ ${t['low_spend']:g} 且点击 ≥ {t['min_clicks']:g}；"
            f"高转化 = 订单 ≥ {t['min_orders_good']:g} 且 ACOS ≤ {t['acos_good']:g}%。同一活动同类提醒每天只发一次。")
    elements += [{"tag": "hr"}, markdown("\n".join(tips)), markdown(rule + " 只读监控，不会修改任何广告。")]
    color = "red" if report["grouped"]["high_no_conv"] or report["grouped"]["high_low_conv"] else "orange"
    value = shell(f"广告实时监控 · 美西 {report['day']} {(report.get('marketplace_time') or '')[-5:]}".strip(),
                  elements, color)
    if card_fits(value):
        return value
    return hourly_card(report, cap=cap // 2) if cap > 2 else None
