"""Feishu 2.0 cards for the SparkX daily report and the 7-day campaign review.

Store and campaign names are untrusted provider text: they only appear in
plain-text table cells or as markdown with link/markup characters neutralised.
"""
import html

from jx_alert.notifications.cards import card_fits, plain, table, with_mentions
from jx_alert.notifications.operators import UNMAPPED_OPERATOR
from jx_alert.sparkx_reports import CATEGORY_LABELS, change, money, percent, ratio, signed

_MD = str.maketrans({"<": "＜", ">": "＞", "[": "［", "]": "］", "(": "（", ")": "）", "*": "＊",
                     "`": "｀", "~": "～", "#": "＃", "|": "｜", "&": "＆", "_": "＿", "\\": "＼"})
_NUMERALS = "一二三四五六七八九十"


def md_safe(value, limit=120):
    return plain(value, limit).translate(_MD)


def _md(text):
    # Template text is ours; untrusted parts were already passed through md_safe.
    return {"tag": "markdown", "content": text}


def _amount(value):
    return "—" if value is None else f"{value:,.2f}"


def _count(value):
    return "—" if value is None else f"{value:,.0f}"


def _shell(title, elements, color="blue"):
    return {"schema": "2.0", "config": {"wide_screen_mode": True},
            "header": {"template": color, "title": {"tag": "plain_text", "content": plain(title, 120)}},
            "body": {"elements": elements}}


def _site_row(label, currency, stores, with_data, current, previous, total_sales):
    current, previous = current or {}, previous or {}
    spend, sales = current.get("spend"), current.get("sales")
    return (label, currency or "—", str(stores), str(with_data), _amount(spend), _amount(sales),
            _count(current.get("units")), _count(current.get("orders")), percent(ratio(spend, sales)),
            "—" if ratio(sales, spend) is None else f"{ratio(sales, spend):.2f}",
            percent(ratio(current.get("orders"), current.get("clicks"))),
            _amount(ratio(spend, current.get("clicks"))), percent(ratio(spend, total_sales)),
            signed(change(spend, previous.get("spend"))), signed(change(sales, previous.get("sales"))))


def daily_card(report, *, quiet_limit=40):
    day, before = report["report_date"], report["previous_date"]
    elements = [_md(f"数据：SparkX get_ads_perf 直出 · 统计日 {day}（店铺当地日期），环比对比 {before}\n"
                    f"授权店铺 {report['profile_count']} 个，当日有花费 {report['with_data']} 个")]
    headers = ["站点", "币种", "店铺数", "有数据", "花费", "销售额", "销量", "订单", "ACOS", "ROAS", "CVR",
               "CPC", "TACOS", "花费环比", "销售额环比"]
    rows = [_site_row(site["label"], site["currency"], site["store_count"], site["with_data"], site["current"],
                      site["previous"], site["total_sales"]) for site in report["sites"]]
    if report["total"]:
        total = report["total"]
        rows.append(_site_row("合计（USD）", "USD", total["store_count"], total["with_data"], total["current"],
                              total["previous"], None))
    elements += [_md(f"**站点汇总 · {day}**"), table(headers, rows)]
    detail = ["店铺", "运营", "花费", "销售额", "销量", "订单", "ACOS", "花费日环比"]
    for site in [site for site in report["sites"] if site["stores"]][:4]:
        elements.append(_md(f"**{site['label']} {len(site['stores'])} 个有花费店铺明细**"))
        elements.append(table(detail, [
            (store["name"], store["operator"] or UNMAPPED_OPERATOR, _amount(store["today"]["spend"]),
             _amount(store["today"]["sales"]), _count(store["today"]["units"]), _count(store["today"]["orders"]),
             percent(ratio(store["today"]["spend"], store["today"]["sales"])),
             "前日为 0" if store["spend_change"] is None and (store["prior"] or {}).get("spend") == 0
             else signed(store["spend_change"])) for store in site["stores"]]))
    quiet = [(site["label"], site["quiet"]) for site in report["sites"] if site["quiet"] and not site["empty"]]
    for label, names in quiet:
        shown = "、".join(md_safe(name, 40) for name in names[:quiet_limit])
        more = f" 等 {len(names)} 个" if len(names) > quiet_limit else ""
        elements.append(_md(f"{label}当日无花费：{shown}{more}"))
    notes = "\n".join(f"{index}. {md_safe(note, 1200)}" for index, note in enumerate(report["notes"], 1))
    elements += [{"tag": "hr"}, _md("**须知**\n" + notes)]
    return _shell(f"SparkX 广告日报 · {day}", elements)


def _within(report, only):
    if only is None:
        return report
    keep = lambda rows: [row for row in rows if row["pid"] in only]  # noqa: E731
    grouped = {code: keep(items) for code, items in report["grouped"].items()}
    return {**report, "findings": keep(report["findings"]), "grouped": grouped,
            "overview": keep(report["overview"]), "zero_stores": keep(report["zero_stores"]),
            "idle_stores": [], "suggestions": None}


def _who(row):
    return row["operator"] or UNMAPPED_OPERATOR


def _overall(site):
    w, p = site["w"] or {}, site["p"] or {}
    acos_w, acos_p = ratio(w.get("spend"), w.get("sales")), ratio(p.get("spend"), p.get("sales"))
    text = (f"{site['label']}站整体花费 {money(w.get('spend'), site['currency'])}"
            f"（{signed(change(w.get('spend'), p.get('spend')))}）、销售 {money(w.get('sales'), site['currency'])}"
            f"（{signed(change(w.get('sales'), p.get('sales')))}）、ACOS {percent(acos_w)}（前 7 日 {percent(acos_p)}）")
    sales = change(w.get("sales"), p.get("sales"))
    if acos_w is not None and acos_p is not None and sales is not None:
        if acos_w - acos_p <= 0.03 and sales >= -0.10:
            text += "——整体数据没有明显问题，问题集中在个别店铺和个别 campaign 上。"
        else:
            text += "——整体效率在下滑，需要重点关注。"
    return text


def _category_rows(code, items):
    rows = []
    for item in items:
        w, p, cur = item["w"] or {}, item["p"] or {}, item["currency"]
        head = (item["store"], _who(item), item["campaign"])
        if code == "zero_orders":
            rows.append((*head, money(w.get("spend"), cur), _count(w.get("clicks"))))
        elif code == "acos_worse":
            rows.append((*head, percent(ratio(p.get("spend"), p.get("sales")), 0) + " → "
                         + percent(ratio(w.get("spend"), w.get("sales")), 0),
                         money(w.get("spend"), cur), money(w.get("sales"), cur)))
        elif code == "surge":
            sales = "归零" if w.get("sales") == 0 else signed(change(w.get("sales"), p.get("sales")))
            rows.append((*head, f"{signed(change(w.get('spend'), p.get('spend')))}（{money(w.get('spend'), cur)}）",
                         sales, money(w.get("spend"), cur)))
        else:
            seen = "无投放记录" if not item["w"] else _count(w.get("impressions"))
            rows.append((*head, money(p.get("spend"), cur), seen))
    return rows


_CATEGORY_TABLES = (
    ("zero_orders", "① 高花费零出单（钱花了，一单没有）", ["店铺", "运营", "Campaign", "近7日花费", "点击"]),
    ("acos_worse", "② ACOS 明显恶化（效率下降）", ["店铺", "运营", "Campaign", "ACOS 前7日→近7日", "近7日花费", "近7日销售"]),
    ("surge", "③ 加预算后销售反而下滑", ["店铺", "运营", "Campaign", "花费变化", "销售变化", "近7日花费"]),
    ("stalled", "④ 异常停摆（enabled 但完全不消耗）", ["店铺", "运营", "Campaign", "前7日花费", "近7日曝光"]),
)


def _anomaly_body(report, *, cap, store_cap, scoped):
    w0, w1 = report["window"]
    p0, p1 = report["previous_window"]
    findings = report["findings"]
    affected = len({item["pid"] for item in findings})
    sites = [site for site in report["sites"] if site["store_count"]]
    main = next((site for site in sites if site["w"]), sites[0] if sites else None)
    label = main["label"] if main and len(sites) == 1 else ""
    if scoped:
        lead = (f"**你负责的店铺中，{affected} 个检出 {len(findings)} 个异常 campaign。**"
                if findings else "**你负责的店铺中没有检出异常 campaign。**")
    else:
        lead = (f"**{report['store_total']} 个{label}店铺中，{report['active_total']} 个有投放数据，"
                f"{affected} 个检出 {len(findings)} 个异常 campaign。**")
    scope = f"统计范围：{w0}~{w1}（近 7 日）对比 {p0}~{p1}（前 7 日），campaign 级。"
    if not scoped:
        scope += "\n" + "\n".join(_overall(site) for site in sites if site["w"])
    elements = [_md(lead + "\n" + scope)]
    sections = []
    if report["zero_stores"]:
        lines = []
        for row in report["zero_stores"][:3]:
            lines.append(f"**{md_safe(row['name'], 40)} 全店零曝光**（运营：{md_safe(_who(row), 40)}）\n"
                         f"{row['enabled']} 个 campaign 全部是 enabled 状态，但近两周 SparkX 没有返回任何曝光、花费或销售记录。"
                         "这不是某个广告表现差，而是整店广告没有投放。常见原因：账户余额或信用卡失效、店铺被暂停、"
                         "Listing 全部下架、预算被清零。建议优先检查，这属于投放中断，而非优化问题。")
        sections.append(("最严重：全店零曝光", [_md("\n\n".join(lines))]))
    if report["idle_stores"] and not scoped:
        names = "、".join(md_safe(row["name"], 30) for row in report["idle_stores"][:20])
        more = f" 等 {len(report['idle_stores'])} 个" if len(report["idle_stores"]) > 20 else ""
        reason = ("未取得活动配置，暂时无法区分是没开广告还是授权范围不含广告" if report["metadata_unavailable"]
                  else "查不到启用的 campaign，更像是没有开广告或授权范围不含广告")
        elements.append(_md(f"另有 {len(report['idle_stores'])} 个店铺零数据（{names}{more}），{reason}，暂不计为异常。"))
    if report["overview"]:
        rows = [(row["name"], _who(row), money((row["w"] or {}).get("spend"), row["currency"]),
                 f"{percent(row['acos_w'], 0)} / {percent(row['acos_p'], 0)}", str(row["count"]), row["problem"])
                for row in report["overview"][:store_cap]]
        body = [table(["店铺", "运营", "近7日花费", "ACOS 近7日/前7日", "异常数", "主要问题"], rows)]
        if len(report["overview"]) > store_cap:
            body.append(_md(f"另有 {len(report['overview']) - store_cap} 个店铺未展开。"))
        sections.append(("异常店铺总览", body))
    category = []
    for code, heading, headers in _CATEGORY_TABLES:
        items = report["grouped"][code]
        if not items:
            continue
        category.append(_md(f"**{heading}**"))
        category.append(table(headers, _category_rows(code, items[:cap])))
        if len(items) > cap:
            category.append(_md(f"另有 {len(items) - cap} 个{CATEGORY_LABELS[code]}活动未展开。"))
    if category:
        sections.append(("按问题类型看具体 campaign", category))
    if report.get("suggestions"):
        sections.append(("建议动作", [_md("\n".join(f"{index}. {md_safe(line, 400)}"
                                               for index, line in enumerate(report["suggestions"], 1)))]))
    for index, (heading, body) in enumerate(sections):
        elements.append(_md(f"**{_NUMERALS[index]}、{heading}**"))
        elements.extend(body)
    elements += [{"tag": "hr"}, _md("数据：SparkX get_ads_perf 直出（campaign 级），近 7 日的订单和销售仍会回补。"
                                    "本报告只读，不会修改任何广告、预算或出价；调整请运营确认后在广告后台操作。")]
    return elements


def anomaly_card(report, *, only=None, mentions=None):
    """Full review for the group/lead; only=profile IDs gives one operator's slice."""
    view = _within(report, only)
    if only is not None and not view["findings"] and not view["zero_stores"]:
        return None
    color = "orange" if view["findings"] or view["zero_stores"] else "green"
    title = "广告异常 · 近 7 日对比前 7 日"
    for cap, store_cap in ((12, 15), (8, 10), (5, 6), (3, 4)):
        value = _shell(title, _anomaly_body(view, cap=cap, store_cap=store_cap, scoped=only is not None), color)
        if mentions:
            value = with_mentions(value, mentions)
        if card_fits(value):
            return value
    return None


def plain_text(card):
    """Readable text dump for previews and tests."""
    parts = [card["header"]["title"]["content"]]
    for element in card["body"]["elements"]:
        if element["tag"] == "markdown":
            parts.append(html.unescape(element["content"]))
        elif element["tag"] == "table":
            names = [column["name"] for column in element["columns"]]
            parts.append(" | ".join(column["display_name"] for column in element["columns"]))
            parts.extend(" | ".join(str(row[name]) for name in names) for row in element["rows"])
    return "\n".join(parts)
