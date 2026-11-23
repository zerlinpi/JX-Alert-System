"""Deterministic analysis of SparkX collections (no model, no invented numbers).

Every figure shown in a notification is computed here from SparkX rows. The
``facts`` lists are the only material DeepSeek may phrase; the ``notes`` and
``actions`` here are the rule-based fallback used when DeepSeek is off,
unavailable or writes a number that is not in the facts.
"""
from collections import defaultdict
import math
import re

from jx_alert.sparkx.collect import METRIC_KEYS
from jx_alert.sparkx.stores import COUNTRY_LABELS, UNMAPPED, display_names

REVIEW_THRESHOLDS = {"zero_spend": 50.0, "zero_clicks": 10, "acos_spend": 50.0, "acos_ratio": 1.5,
                     "acos_delta": 0.15, "surge_prev_spend": 30.0, "surge_spend": 50.0, "surge_up": 0.30,
                     "surge_sales_down": -0.20, "stall_prev_spend": 50.0, "zero_bid_cut": 0.30,
                     "min_bid_clicks": 10, "opportunity_max_spend": 100.0, "opportunity_min_orders": 3,
                     "opportunity_max_acos": 0.20}
HOURLY_DEFAULTS = {"high_spend": 30.0, "low_spend": 10.0, "min_clicks": 15, "acos_high": 60.0,
                   "cvr_low": 5.0, "acos_good": 20.0, "min_orders_good": 2}
REVIEW_LABELS = {"zero_stores": "全店零曝光", "zero_orders": "高花费零出单", "acos_worse": "ACOS 明显恶化",
                 "surge": "加预算后销售反而下滑", "stalled": "异常停摆"}
REVIEW_ORDER = ("zero_orders", "acos_worse", "surge", "stalled")
HOURLY_LABELS = {"high_no_conv": "高花费无转化", "high_low_conv": "高花费低转化",
                 "low_no_conv": "低花费无转化", "low_high_conv": "低花费高转化"}
HOURLY_ORDER = ("high_no_conv", "high_low_conv", "low_no_conv", "low_high_conv")


# ---------------------------------------------------------------- arithmetic and formatting

def total(rows, keys=METRIC_KEYS):
    rows = [row for row in rows if row is not None]
    if not rows:
        return None
    result = {}
    for key in keys:
        values = [row.get(key) for row in rows]
        result[key] = None if any(value is None for value in values) else math.fsum(values)
    return result


def ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def change(current, previous):
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous


def money(value, currency, *, cents=None):
    if value is None:
        return "—"
    cents = abs(value) < 100 if cents is None else cents
    text = f"{value:,.2f}" if cents else f"{value:,.0f}"
    return f"${text}" if currency == "USD" else f"{text} {currency or ''}".strip()


def amount(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def count(value):
    return "—" if value is None else f"{value:,.0f}"


def percent(value, digits=1):
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def signed(value, digits=1):
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def times(value):
    return "—" if value is None else f"{value:.2f}"


def kpis(values, business=None):
    """Derived ratios from summed base metrics (never averaged ratios)."""
    values = values or {}
    business = business or {}
    return {"spend": values.get("spend"), "sales": values.get("sales"), "orders": values.get("orders"),
            "units": values.get("units"), "clicks": values.get("clicks"), "impressions": values.get("impressions"),
            "acos": ratio(values.get("spend"), values.get("sales")),
            "roas": ratio(values.get("sales"), values.get("spend")),
            "cvr": ratio(values.get("orders"), values.get("clicks")),
            "cpc": ratio(values.get("spend"), values.get("clicks")),
            "ctr": ratio(values.get("clicks"), values.get("impressions")),
            "total_sales": business.get("total_sales"), "total_orders": business.get("total_orders"),
            "tacos": ratio(business.get("tacos_spend"), business.get("tacos_sales"))}


def _label(country):
    return COUNTRY_LABELS.get(country, country)


# ---------------------------------------------------------------- daily report

def _fsum(values):
    return None if not values or None in values else math.fsum(values)


def _business_totals(stores, slot):
    """Total sales/orders; TACOS only over stores with both ad and business rows (one scope)."""
    rows = [(store[slot], store["business_" + slot]) for store in stores if store["business_" + slot]]
    if not rows:
        return None
    both = [(ads, business) for ads, business in rows if ads is not None]
    return {"total_sales": _fsum([business["total_sales"] for _, business in rows]),
            "total_orders": _fsum([business["total_orders"] for _, business in rows]),
            "tacos_spend": _fsum([ads.get("spend") for ads, _ in both]),
            "tacos_sales": _fsum([business["total_sales"] for _, business in both]),
            "stores": len(rows), "tacos_stores": len(both)}


def build_daily(collected, people):
    """Company KPIs, per-site totals and per-store rows for one completed day."""
    names = display_names(collected["profiles"])
    day, before = collected["report_date"], collected["previous_date"]
    daily, business = collected.get("daily") or {}, collected.get("business")
    currency = collected.get("currency")
    stores = []
    for key, profile in collected["profiles"].items():
        today, prior = daily.get(key, {}).get(day), daily.get(key, {}).get(before)
        stores.append({
            "pid": key, "name": names[key], "country": profile["country"],
            "operator": people.get(key) or UNMAPPED, "current": today, "previous": prior,
            "business_current": (business or {}).get(key, {}).get(day) if business is not None else None,
            "business_previous": (business or {}).get(key, {}).get(before) if business is not None else None})
    for store in stores:
        store["kpi"] = kpis(store["current"], _business_totals([store], "current"))
        store["spend_change"] = change((store["current"] or {}).get("spend"), (store["previous"] or {}).get("spend"))
    current = total(store["current"] for store in stores)
    previous = total(store["previous"] for store in stores)
    company = {"current": kpis(current, _business_totals(stores, "current")),
               "previous": kpis(previous, _business_totals(stores, "previous")),
               "store_count": len(stores),
               "with_rows": sum(store["current"] is not None for store in stores),
               "previous_rows": sum(store["previous"] is not None for store in stores),
               "with_spend": sum(((store["current"] or {}).get("spend") or 0) > 0 for store in stores),
               "business_stores": sum(store["business_current"] is not None for store in stores),
               "previous_business_stores": sum(store["business_previous"] is not None for store in stores),
               "business_available": business is not None}
    sites = []
    for country in sorted({store["country"] for store in stores},
                          key=lambda code: (code != "US", code)):
        members = [store for store in stores if store["country"] == country]
        cur, pre = total(s["current"] for s in members), total(s["previous"] for s in members)
        spenders = sorted((s for s in members if ((s["current"] or {}).get("spend") or 0) > 0),
                          key=lambda s: -s["current"]["spend"])
        sites.append({"code": country, "label": _label(country), "store_count": len(members),
                      "with_data": len(spenders), "current": kpis(cur), "previous": kpis(pre),
                      "spend_change": change((cur or {}).get("spend"), (pre or {}).get("spend")),
                      "sales_change": change((cur or {}).get("sales"), (pre or {}).get("sales")),
                      "empty": all(s["current"] is None for s in members), "stores": spenders,
                      "quiet": [s["name"] for s in members if s["current"] is not None and s not in spenders],
                      "no_rows": [s["name"] for s in members if s["current"] is None]})
    report = {"kind": "daily", "report_date": day, "previous_date": before, "currency": currency,
              "company": company, "sites": sites, "stores": stores,
              "missing": [collected["roster"].get(key, {}).get("name", key) for key in collected.get("missing", [])],
              "errors": dict(collected.get("errors") or {})}
    report["facts"] = daily_facts(report)
    report["notes"] = daily_notes(report)
    return report


def daily_facts(report):
    """Observations with their exact display figures; the material for narrative text."""
    cur = report["currency"]
    company, facts = report["company"], []
    now, before = company["current"], company["previous"]
    facts.append({"kind": "company", "text": (
        f"全公司广告花费 {money(now['spend'], cur)}（前一日 {money(before['spend'], cur)}，"
        f"{signed(change(now['spend'], before['spend']))}），广告销售额 {money(now['sales'], cur)}"
        f"（{signed(change(now['sales'], before['sales']))}），ACOS {percent(now['acos'])}，"
        f"CVR {percent(now['cvr'])}，PPC {money(now['cpc'], cur, cents=True)}")})
    if now["total_sales"] is not None:
        facts.append({"kind": "business", "text": (
            f"店铺总销售额 {money(now['total_sales'], cur)}、总订单 {count(now['total_orders'])}，"
            f"TACOS {percent(now['tacos'])}（前一日 {percent(before['tacos'])}）")})
    empty = [site for site in report["sites"] if site["empty"]]
    if empty:
        facts.append({"kind": "empty_sites", "text": (
            f"{'、'.join(site['label'] for site in empty)}共 {sum(site['store_count'] for site in empty)} 个店铺"
            "当日 SparkX 没有返回任何广告数据（不是 0，而是查不到记录）")})
    main = max(report["sites"], key=lambda site: site["current"]["spend"] or 0, default=None)
    if main and main["stores"]:
        spend = main["current"]["spend"]
        top = main["stores"][0]
        share = ratio(top["current"]["spend"], spend)
        if share is not None and share >= 0.25 and len(main["stores"]) > 1:
            facts.append({"kind": "concentration", "store": top["name"], "text": (
                f"{top['name']} 一家店占{main['label']}站花费的 {percent(share, 0)}（{money(top['current']['spend'], cur)}），"
                f"销售额占 {percent(ratio(top['current']['sales'], main['current']['sales']), 0)}"
                f"（{money(top['current']['sales'], cur)}），ACOS {percent(top['kpi']['acos'])}")})
        rated = [s for s in main["stores"] if s["current"]["spend"] >= 50 and s["kpi"]["acos"] is not None]
        ranked = sorted(rated, key=lambda s: -s["kpi"]["acos"])
        for index, store in enumerate(ranked[:2]):
            if store["kpi"]["acos"] >= 0.4:
                facts.append({"kind": "high_acos", "store": store["name"], "text": (
                    f"{store['name']} 花费 {money(store['current']['spend'], cur)}，只带回 "
                    f"{money(store['current']['sales'], cur)}，ACOS {percent(store['kpi']['acos'])}"
                    + ("，为全站最高" if index == 0 else "，排名第二高"))})
        for store in main["stores"]:
            if store["current"]["spend"] >= 50 and store["current"]["sales"] == 0:
                clicks = store["current"]["clicks"]
                facts.append({"kind": "zero_sales", "store": store["name"], "text": (
                    f"{store['name']} 花费 {money(store['current']['spend'], cur)}，销售额为 0"
                    + (f"，{count(clicks)} 次点击没有出单" if clicks else ""))})
        if len(ranked) > 1 and ranked[-1]["pid"] != top["pid"]:
            best = ranked[-1]
            facts.append({"kind": "best_acos", "store": best["name"], "text": (
                f"{best['name']} ACOS {percent(best['kpi']['acos'])}（花费 {money(best['current']['spend'], cur)}），"
                "是全站效率最好的")})
        spread = (ranked[0]["kpi"]["acos"] - ranked[-1]["kpi"]["acos"]) if len(ranked) > 1 else 0
        if spread >= 0.2:
            facts.append({"kind": "spread", "text": (
                f"{main['label']}站 ACOS {percent(main['current']['acos'])}，店铺间分化较大（最高 "
                f"{percent(ranked[0]['kpi']['acos'])}，最低 {percent(ranked[-1]['kpi']['acos'])}）")})
        for store in main["stores"]:
            moved = store["spend_change"]
            if store["current"]["spend"] >= 100 and moved is not None and moved >= 0.5:
                facts.append({"kind": "spend_jump", "store": store["name"], "text": (
                    f"{store['name']} 花费较前一日 {signed(moved, 0)}（{money((store['previous'] or {}).get('spend'), cur)}"
                    f" → {money(store['current']['spend'], cur)}）")})
    if report["missing"]:
        facts.append({"kind": "unauthorized", "text": (
            f"{len(report['missing'])} 个已开启监控的店铺不在当前 Token 授权内，已跳过")})
    if report["errors"].get("business"):
        facts.append({"kind": "business_missing", "text": "SparkX 未返回店铺总销售额，总销售额与 TACOS 暂不显示"})
    return facts[:14]


def daily_notes(report):
    """Rule-based 须知 (fallback): site coverage first, then the main site's efficiency."""
    by_kind = defaultdict(list)
    for fact in report["facts"]:
        by_kind[fact["kind"]].append(fact["text"])
    notes = []
    if by_kind["empty_sites"]:
        notes.append(by_kind["empty_sites"][0] + "。如果这些站点确实在投放，最可能是 Token 的店铺范围只勾选了部分站点，"
                     "请到 SparkX 后台「MCP & Skills」核对该 Token 的授权范围。")
    detail = [*by_kind["spread"], *by_kind["concentration"], *by_kind["high_acos"], *by_kind["zero_sales"],
              *by_kind["best_acos"], *by_kind["spend_jump"]]
    if detail:
        notes.append("；".join(detail[:6]) + "。")
    tail = [*by_kind["unauthorized"], *by_kind["business_missing"]]
    if tail:
        notes.append("；".join(tail) + "。")
    return notes or ["当日各店数据未见需要单独说明的波动。"]


# ---------------------------------------------------------------- 7-day review

def _bid(cpc, factor):
    if cpc is None or factor is None or not math.isfinite(factor) or factor <= 0:
        return None
    return round(cpc * factor, 2)


def _classify(item, usd, t):
    w, p = item.get("w") or {}, item.get("p") or {}
    ws, wc, wo, wsl = w.get("spend"), w.get("clicks"), w.get("orders"), w.get("sales")
    ps, psl = p.get("spend"), p.get("sales")
    if not usd:
        return None
    if item.get("state") == "enabled" and ps is not None and ps >= t["stall_prev_spend"] and (
            not item.get("w") or w.get("impressions") == 0):
        return "stalled"
    up = change(ws, ps)
    if ws is not None and ws >= t["zero_spend"] and wc is not None and wc >= t["zero_clicks"] and wo == 0:
        if psl and up is not None and up >= t["surge_up"]:
            return "surge"
        return "zero_orders"
    down = change(wsl, psl)
    if (ws is not None and ws >= t["surge_spend"] and ps is not None and ps >= t["surge_prev_spend"]
            and up is not None and up >= t["surge_up"] and down is not None and down <= t["surge_sales_down"]):
        return "surge"
    acos_w, acos_p = ratio(ws, wsl), ratio(ps, psl)
    if (ws is not None and ws >= t["acos_spend"] and acos_w is not None and acos_p is not None
            and acos_w >= acos_p * t["acos_ratio"] and acos_w - acos_p >= t["acos_delta"]):
        return "acos_worse"
    return None


def _opportunity(item, t):
    """低花费高转化: little spend, several orders and a low ACOS in the last 7 days."""
    w = item.get("w") or {}
    acos = ratio(w.get("spend"), w.get("sales"))
    return (item.get("state") in (None, "enabled") and w.get("spend") is not None and 0 < w["spend"] < t["opportunity_max_spend"]
            and (w.get("orders") or 0) >= t["opportunity_min_orders"] and acos is not None
            and acos <= t["opportunity_max_acos"])


def _advice(item, t):
    """A concrete, explainable bid suggestion from the campaign's own numbers."""
    w, p = item["w"] or {}, item["p"] or {}
    clicks = w.get("clicks")
    cpc = ratio(w.get("spend"), clicks)
    if clicks is None or clicks < t["min_bid_clicks"]:
        cpc = None
    if item["category"] == "acos_worse":
        acos_w, acos_p = ratio(w.get("spend"), w.get("sales")), ratio(p.get("spend"), p.get("sales"))
        target = _bid(cpc, ratio(acos_p, acos_w))
        return {"cpc": cpc, "target": target, "basis": f"按前 7 日 ACOS {percent(acos_p, 0)} 反推"}
    if item["category"] == "zero_orders":
        return {"cpc": cpc, "target": _bid(cpc, 1 - t["zero_bid_cut"]),
                "basis": f"零出单先降约 {t['zero_bid_cut'] * 100:.0f}% 并排查搜索词"}
    return {"cpc": cpc, "target": None, "basis": None}


def build_review(collected, people, thresholds=None):
    t = {**REVIEW_THRESHOLDS, **(thresholds or {})}
    names = display_names(collected["profiles"])
    campaigns = collected.get("campaigns") or {}
    enabled = collected.get("enabled_campaigns")
    currency = collected.get("currency")
    usd = currency == "USD"
    findings, zero_stores, idle_stores, stores, opportunities = [], [], [], {}, []
    sites = defaultdict(lambda: {"store_count": 0, "active": 0, "w": [], "p": []})
    for key, profile in collected["profiles"].items():
        site = sites[profile["country"]]
        site["store_count"] += 1
        items = campaigns.get(key, {})
        site["w"] += [item["w"] for item in items.values() if item["w"]]
        site["p"] += [item["p"] for item in items.values() if item["p"]]
        delivering = any(((item["w"] or {}).get("impressions") or 0) > 0 for item in items.values())
        site["active"] += delivering
        touched = delivering or any(((item["p"] or {}).get("impressions") or 0) > 0 for item in items.values())
        operator = people.get(key) or UNMAPPED
        if not touched:
            enabled_count = None if enabled is None else enabled.get(key)
            row = {"pid": key, "name": names[key], "operator": operator, "enabled": enabled_count,
                   "country": profile["country"]}
            (zero_stores if enabled_count else idle_stores).append(row)
            continue
        store_w = total(item["w"] for item in items.values() if item["w"])
        store_p = total(item["p"] for item in items.values() if item["p"])
        stores[key] = {"pid": key, "name": names[key], "operator": operator, "w": store_w, "p": store_p,
                       "acos_w": ratio((store_w or {}).get("spend"), (store_w or {}).get("sales")),
                       "acos_p": ratio((store_p or {}).get("spend"), (store_p or {}).get("sales"))}
        for cid, item in items.items():
            category = _classify(item, usd, t)
            if category:
                finding = {"pid": key, "store": names[key], "operator": operator, "cid": cid,
                           "campaign": item["name"], "type": item["type"], "category": category,
                           "w": item["w"], "p": item["p"]}
                finding["advice"] = _advice(finding, t)
                findings.append(finding)
            elif usd and _opportunity(item, t):
                opportunities.append({"pid": key, "store": names[key], "operator": operator, "cid": cid,
                                      "campaign": item["name"], "w": item["w"], "p": item["p"],
                                      "acos": ratio(item["w"]["spend"], item["w"]["sales"])})
    overview = []
    by_store = defaultdict(list)
    for item in findings:
        by_store[item["pid"]].append(item)
    for key, items in by_store.items():
        store = stores[key]
        counts = defaultdict(int)
        for item in items:
            counts[item["category"]] += 1
        trend = []
        spend_change = change((store["w"] or {}).get("spend"), (store["p"] or {}).get("spend"))
        sales_change = change((store["w"] or {}).get("sales"), (store["p"] or {}).get("sales"))
        if (sales_change is not None and sales_change <= -0.2) or (spend_change is not None and spend_change >= 0.3):
            trend.append(f"花费 {signed(spend_change, 0)}、销售 {signed(sales_change, 0)}")
        summary = "、".join(f"{counts[code]} 个{REVIEW_LABELS[code]}" for code in REVIEW_ORDER if counts[code])
        overview.append({**store, "count": len(items), "problem": "；".join([*trend, summary]),
                         "spend_change": spend_change, "sales_change": sales_change})
    overview.sort(key=lambda row: (-row["count"], -((row["w"] or {}).get("spend") or 0)))
    grouped = {code: [] for code in REVIEW_ORDER}
    for item in findings:
        grouped[item["category"]].append(item)
    for code, items in grouped.items():
        items.sort(key=lambda item, code=code: -(((item["p"] if code == "stalled" else item["w"]) or {})
                                                 .get("spend") or 0))
    site_rows = []
    for country, site in sorted(sites.items(), key=lambda kv: (kv[0] != "US", kv[0])):
        w, p = total(site["w"]), total(site["p"])
        site_rows.append({"code": country, "label": _label(country), "store_count": site["store_count"],
                          "active": site["active"], "w": w, "p": p,
                          "acos_w": ratio((w or {}).get("spend"), (w or {}).get("sales")),
                          "acos_p": ratio((p or {}).get("spend"), (p or {}).get("sales"))})
    opportunities.sort(key=lambda item: (item["acos"], -(item["w"].get("orders") or 0)))
    report = {"kind": "review", "window": collected["window"], "previous_window": collected["previous_window"],
              "currency": currency, "usd": usd, "sites": site_rows, "findings": findings, "grouped": grouped,
              "opportunities": opportunities,
              "overview": overview, "zero_stores": zero_stores, "idle_stores": idle_stores,
              "store_total": len(collected["profiles"]), "active_total": sum(s["active"] for s in site_rows),
              "affected": len(by_store), "metadata_unavailable": enabled is None,
              "missing": [collected["roster"].get(key, {}).get("name", key) for key in collected.get("missing", [])],
              "errors": dict(collected.get("errors") or {})}
    report["facts"] = review_facts(report)
    report["actions"] = review_actions(report)
    return report


def _label_campaign(name, limit=40):
    text = str(name or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def review_facts(report):
    cur, facts = report["currency"], []
    for site in report["sites"]:
        if site["w"]:
            w, p = site["w"], site["p"] or {}
            facts.append({"kind": "site", "text": (
                f"{site['label']}站近 7 日花费 {money(w.get('spend'), cur)}（{signed(change(w.get('spend'), p.get('spend')))}），"
                f"销售 {money(w.get('sales'), cur)}（{signed(change(w.get('sales'), p.get('sales')))}），"
                f"ACOS {percent(site['acos_w'])}（前 7 日 {percent(site['acos_p'])}）")})
    for row in report["zero_stores"][:3]:
        facts.append({"kind": "zero_store", "store": row["name"], "text": (
            f"{row['name']} 全店零曝光，{row['enabled']} 个 campaign 处于开启状态，近两周没有任何曝光、花费或销售记录")})
    for code in REVIEW_ORDER:
        items = report["grouped"][code]
        if items:
            facts.append({"kind": "count_" + code, "text": f"{REVIEW_LABELS[code]} {len(items)} 个"})
        for item in items[:3]:
            w, p, advice = item["w"] or {}, item["p"] or {}, item["advice"]
            name = _label_campaign(item["campaign"], 60)
            if code == "zero_orders":
                text = (f"{item['store']} 的 {name}：近 7 日花费 {money(w.get('spend'), cur)}、"
                        f"{count(w.get('clicks'))} 次点击零出单")
            elif code == "acos_worse":
                text = (f"{item['store']} 的 {name}：ACOS {percent(ratio(p.get('spend'), p.get('sales')), 0)} → "
                        f"{percent(ratio(w.get('spend'), w.get('sales')), 0)}，近 7 日花费 {money(w.get('spend'), cur)}")
            elif code == "surge":
                text = (f"{item['store']} 的 {name}：花费 {signed(change(w.get('spend'), p.get('spend')), 0)}、"
                        f"销售 {signed(change(w.get('sales'), p.get('sales')), 0)}")
            else:
                text = f"{item['store']} 的 {name}：前 7 日花费 {money(p.get('spend'), cur)}，近 7 日没有曝光"
            if advice.get("target") is not None:
                text += f"，建议 CPC {advice['cpc']:.2f} → {advice['target']:.2f}（{advice['basis']}）"
            facts.append({"kind": code, "store": item["store"], "text": text})
    if report["opportunities"]:
        facts.append({"kind": "count_opportunity", "text": f"低花费高转化 {len(report['opportunities'])} 个"})
    for item in report["opportunities"][:3]:
        w = item["w"]
        facts.append({"kind": "opportunity", "store": item["store"], "text": (
            f"{item['store']} 的 {_label_campaign(item['campaign'], 60)}：近 7 日花费 {money(w.get('spend'), cur)}、"
            f"{count(w.get('orders'))} 单，ACOS {percent(item['acos'])}，可适当加预算或出价放量")})
    if report["idle_stores"]:
        facts.append({"kind": "idle", "text": f"另有 {len(report['idle_stores'])} 个店铺两周零数据，暂不计为异常"})
    if not report["usd"] and report["currency"]:
        facts.append({"kind": "currency", "text": f"金额币种为 {report['currency']}，按美元阈值的异常判定未执行"})
    return facts[:24]


def review_actions(report):
    """Rule-based 建议动作 (fallback), each tied to one store."""
    cur, actions = report["currency"], []
    for row in report["zero_stores"][:3]:
        actions.append({"store": row["name"], "text": (
            f"{row['name']} 先查账户状态（余额/信用卡、店铺是否暂停、Listing 是否下架、预算是否清零），"
            "这是“全店停投”，损失最大。")})
    worse = defaultdict(list)
    for item in report["grouped"]["acos_worse"]:
        worse[item["pid"]].append(item)
    for _, items in sorted(worse.items(), key=lambda kv: -sum((i["w"] or {}).get("spend") or 0 for i in kv[1]))[:2]:
        first = items[0]
        advice = first["advice"]
        bid = (f"，平均 CPC 由 {advice['cpc']:.2f} 降到约 {advice['target']:.2f}（{advice['basis']}）"
               if advice.get("target") is not None else "")
        actions.append({"store": first["store"], "text": (
            f"{first['store']} 的 {len(items)} 个 ACOS 恶化活动（如 {_label_campaign(first['campaign'], 30)}）"
            f"建议复核出价和广告位溢价{bid}。")})
    for item in report["grouped"]["zero_orders"][:2]:
        w, advice = item["w"], item["advice"]
        test = re.search(r"test|测试", item["campaign"] or "", re.IGNORECASE)
        bid = (f"，出价先降到约 {advice['target']:.2f}（现 CPC {advice['cpc']:.2f}）"
               if advice.get("target") is not None else "")
        actions.append({"store": item["store"], "text": (
            f"{item['store']} {_label_campaign(item['campaign'], 36)}：{count(w['clicks'])} 次点击零转化、"
            f"花费 {money(w['spend'], cur)}，建议排查搜索词并否定无效词{bid}"
            + ("；名称含 TEST/测试，可考虑直接归档。" if test else "。"))})
    for item in report["grouped"]["surge"][:1]:
        actions.append({"store": item["store"], "text": (
            f"{item['store']} {_label_campaign(item['campaign'], 36)} 花费 "
            f"{signed(change(item['w']['spend'], item['p']['spend']), 0)}、销售 "
            f"{signed(change(item['w']['sales'], item['p']['sales']), 0)}，放量没有带来销售，建议收回新增预算。")})
    for item in report["grouped"]["stalled"][:1]:
        actions.append({"store": item["store"], "text": (
            f"{item['store']} {_label_campaign(item['campaign'], 36)} 前 7 日花费 "
            f"{money(item['p']['spend'], cur)}、近 7 日无曝光：查预算是否改为 0、是否断货或被限流。")})
    return actions[:6]


def review_slice(report, only):
    """One operator's view: only their stores, without the company totals."""
    def keep(rows):
        return [row for row in rows if row["pid"] in only]

    grouped = {code: keep(items) for code, items in report["grouped"].items()}
    names = {row["name"] for row in report["overview"] + report["zero_stores"] if row["pid"] in only}
    return {**report, "findings": keep(report["findings"]), "grouped": grouped,
            "overview": keep(report["overview"]), "zero_stores": keep(report["zero_stores"]),
            "opportunities": keep(report["opportunities"]),
            "idle_stores": [], "affected": len({row["pid"] for row in keep(report["findings"])}),
            "actions": [row for row in report["actions"] if row.get("store") in names], "scoped": True}


# ---------------------------------------------------------------- hourly monitor

def classify_hourly(values, t):
    spend, clicks, orders, sales = (values.get(k) for k in ("spend", "clicks", "orders", "sales"))
    if None in (spend, clicks, orders):
        return None
    acos, cvr = ratio(spend, sales), ratio(orders, clicks)
    if spend >= t["high_spend"]:
        if orders == 0 and clicks >= 1:
            return "high_no_conv"
        if orders > 0 and ((acos is not None and acos * 100 >= t["acos_high"])
                           or (cvr is not None and cvr * 100 < t["cvr_low"])):
            return "high_low_conv"
        return None
    if orders == 0 and spend >= t["low_spend"] and clicks >= t["min_clicks"]:
        return "low_no_conv"
    if orders >= t["min_orders_good"] and acos is not None and acos * 100 <= t["acos_good"]:
        return "low_high_conv"
    return None


def finding_key(item):
    return f"{item['pid']}:{item['cid']}:{item['category']}"


def build_hourly(collected, people, thresholds=None, alerted=()):
    t = {**HOURLY_DEFAULTS, **(thresholds or {})}
    names = display_names(collected["profiles"])
    currency = collected.get("currency")
    findings, rows = [], []
    for key in collected["profiles"]:
        for cid, item in (collected.get("campaigns") or {}).get(key, {}).items():
            values = item.get("t") or {}
            rows.append(values)
            if currency != "USD" or item.get("state") not in (None, "enabled"):
                continue
            category = classify_hourly(values, t)
            if category:
                findings.append({"pid": key, "store": names[key], "operator": people.get(key) or UNMAPPED,
                                 "cid": cid, "campaign": item["name"], "type": item["type"],
                                 "category": category, "t": values, "kpi": kpis(values)})
    findings.sort(key=lambda item: (HOURLY_ORDER.index(item["category"]), -(item["t"].get("spend") or 0)))
    seen = set(alerted)
    new = [item for item in findings if finding_key(item) not in seen]
    grouped = {code: [item for item in new if item["category"] == code] for code in HOURLY_ORDER}
    return {"kind": "hourly", "day": collected["day"], "marketplace_time": collected.get("marketplace_time"),
            "currency": currency, "thresholds": t, "findings": findings, "new": new, "grouped": grouped,
            "store_count": len(collected["profiles"]), "totals": kpis(total(rows)),
            "usd": currency == "USD", "errors": dict(collected.get("errors") or {})}


def hourly_slice(report, only):
    new = [item for item in report["new"] if item["pid"] in only]
    return {**report, "new": new, "grouped": {code: [i for i in new if i["category"] == code] for code in HOURLY_ORDER},
            "scoped": True}
