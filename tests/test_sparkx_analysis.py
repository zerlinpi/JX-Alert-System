"""Deterministic daily, review and hourly analysis of SparkX collections."""
import pytest

from jx_alert.sparkx import analysis

DAY, BEFORE = "2026-11-22", "2026-11-21"
W, P = ["2026-11-16", "2026-11-22"], ["2026-11-09", "2026-11-15"]
PROFILES = {"101": {"name": "香港川页", "country": "US", "store_type": "seller"},
            "102": {"name": "Campx-主账号", "country": "US", "store_type": "seller"},
            "103": {"name": "FITFIZZ-主账号", "country": "US", "store_type": "seller"},
            "104": {"name": "陈子俊", "country": "US", "store_type": "seller"},
            "105": {"name": "昌泉", "country": "US", "store_type": "seller"},
            "106": {"name": "迈源", "country": "US", "store_type": "seller"},
            "201": {"name": "川页-CA", "country": "CA", "store_type": "seller"}}
PEOPLE = {"101": "皮泽霖", "102": "高爽"}


def m(impressions, clicks, spend, sales, orders, units=None):
    return {"impressions": float(impressions), "clicks": float(clicks), "spend": float(spend),
            "sales": float(sales), "orders": float(orders), "units": float(orders if units is None else units)}


def business(sales, orders):
    return {"total_sales": float(sales), "total_orders": float(orders), "total_units": float(orders)}


DAILY = {"kind": "daily", "report_date": DAY, "previous_date": BEFORE, "currency": "USD", "errors": {},
         "roster": PROFILES, "profiles": PROFILES, "disabled": [], "missing": [],
         "daily": {"101": {DAY: m(20000, 1500, 3217.70, 10726.72, 239, 256), BEFORE: m(18000, 1300, 2616.0, 9000, 200)},
                   "102": {DAY: m(9000, 300, 598.32, 927.93, 7), BEFORE: m(8000, 260, 494.48, 900, 6)},
                   "103": {DAY: m(4000, 120, 184.25, 0, 0), BEFORE: m(1500, 40, 61.2, 0, 0)},
                   "104": {DAY: m(800, 20, 33.48, 179.99, 1), BEFORE: m(700, 18, 31.0, 160, 1)},
                   "105": {DAY: m(0, 0, 0, 0, 0)}},
         "business": {"101": {DAY: business(20000, 400), BEFORE: business(18000, 350)},
                      "102": {DAY: business(1500, 30), BEFORE: business(1400, 28)},
                      "103": {DAY: business(300, 6), BEFORE: business(280, 5)},
                      "104": {DAY: business(400, 8), BEFORE: business(380, 7)}, "106": {DAY: business(250, 5)}}}


def test_company_kpis_use_summed_base_metrics_and_one_tacos_scope():
    report = analysis.build_daily(DAILY, PEOPLE)
    now = report["company"]["current"]
    spend = 3217.70 + 598.32 + 184.25 + 33.48
    assert now["spend"] == pytest.approx(spend)
    assert now["acos"] == pytest.approx(spend / (10726.72 + 927.93 + 0 + 179.99))
    assert now["cvr"] == pytest.approx((239 + 7 + 0 + 1) / (1500 + 300 + 120 + 20))
    assert now["cpc"] == pytest.approx(spend / 1940)
    # 106 has total sales but no ad row: it counts in 店铺总销售额 but not in the TACOS scope.
    assert now["total_sales"] == pytest.approx(22450) and now["total_orders"] == pytest.approx(449)
    assert now["tacos"] == pytest.approx(spend / 22200)
    company = report["company"]
    assert (company["store_count"], company["with_rows"], company["with_spend"]) == (7, 5, 4)


def test_sites_keep_missing_as_missing_and_list_quiet_stores():
    report = analysis.build_daily(DAILY, PEOPLE)
    us, ca = report["sites"]
    assert (us["code"], us["store_count"], us["with_data"]) == ("US", 6, 4)
    assert [store["name"] for store in us["stores"]] == ["香港川页", "Campx", "FITFIZZ", "陈子俊"]
    assert us["quiet"] == ["昌泉"] and us["no_rows"] == ["迈源"]
    assert ca["empty"] and ca["current"]["spend"] is None and ca["with_data"] == 0
    assert us["stores"][0]["operator"] == "皮泽霖" and us["stores"][2]["operator"] == "未映射运营"
    assert us["stores"][0]["spend_change"] == pytest.approx(3217.70 / 2616 - 1)


def test_daily_facts_and_rule_notes_in_the_agent_tone():
    report = analysis.build_daily(DAILY, PEOPLE)
    text = "\n".join(fact["text"] for fact in report["facts"])
    assert "加拿大共 1 个店铺当日 SparkX 没有返回任何广告数据（不是 0，而是查不到记录）" in text
    assert "香港川页 一家店占美国站花费的 80%（$3,218）" in text
    assert "Campx 花费 $598，只带回 $928，ACOS 64.5%，为全站最高" in text
    assert "FITFIZZ 花费 $184，销售额为 0，120 次点击没有出单" in text
    notes = report["notes"]
    assert notes[0].startswith("加拿大共 1 个店铺") and "MCP & Skills" in notes[0]
    assert "店铺间分化较大" in notes[1]


def campaign(name, w, p, state="enabled", kind="sponsoredProducts"):
    return {"name": name, "type": kind, "state": state, "w": w, "p": p}


REVIEW = {"kind": "review", "window": W, "previous_window": P, "currency": "USD", "errors": {},
          "roster": PROFILES, "profiles": PROFILES, "disabled": [], "missing": [],
          "campaigns": {
              "101": {"1": campaign("14MQ-S-SP-queen bed platform", m(9000, 120, 162, 93, 2), m(8000, 110, 150, 484, 9)),
                      "2": campaign("S-14MQ-metal bed frame", None, m(5000, 90, 126, 400, 8)),
                      "3": campaign("Brand-Exact", m(50000, 1500, 1100, 3300, 60), m(48000, 1450, 1000, 3000, 55))},
              "102": {"4": campaign("CTH-ASIN定投-TEST", m(3000, 95, 223, 0, 0), m(2000, 50, 100, 0, 0)),
                      "5": campaign("CTH-1025-SD", m(4000, 120, 150, 325, 5), m(3000, 90, 100, 500, 8),
                                    kind="sponsoredDisplay")},
              "103": {"6": campaign("Asin定投 [x](https://evil.example) <at id=ou_x>", m(6000, 313, 303, 0, 0), None)},
              "104": {"7": campaign("陈子俊-Auto", m(2000, 60, 290, 1620, 9), m(2100, 62, 280, 1500, 8))}},
          "enabled_campaigns": {"105": 44, "106": 0, "201": 0}}


def test_review_categories_overview_and_bid_advice():
    report = analysis.build_review(REVIEW, PEOPLE)
    found = {item["cid"]: item["category"] for item in report["findings"]}
    assert found == {"1": "acos_worse", "2": "stalled", "4": "zero_orders", "5": "surge", "6": "zero_orders"}
    assert [row["pid"] for row in report["zero_stores"]] == ["105"]
    assert {row["pid"] for row in report["idle_stores"]} == {"106", "201"}
    assert (report["store_total"], report["active_total"], report["affected"]) == (7, 4, 3)
    overview = {row["pid"]: row for row in report["overview"]}
    assert overview["101"]["problem"] == "1 个ACOS 明显恶化、1 个异常停摆"
    worse = next(item for item in report["findings"] if item["cid"] == "1")
    acos_w, acos_p = 162 / 93, 150 / 484
    assert worse["advice"]["cpc"] == pytest.approx(162 / 120)
    assert worse["advice"]["target"] == round(162 / 120 * acos_p / acos_w, 2)
    zero = next(item for item in report["findings"] if item["cid"] == "4")
    assert zero["advice"]["target"] == round(223 / 95 * 0.7, 2)
    assert [item["cid"] for item in report["grouped"]["zero_orders"]] == ["6", "4"]  # by spend


def test_review_actions_and_operator_slice():
    report = analysis.build_review(REVIEW, PEOPLE)
    actions = report["actions"]
    assert actions[0]["store"] == "昌泉" and "全店停投" in actions[0]["text"]
    assert any("TEST/测试" in row["text"] for row in actions)
    scoped = analysis.review_slice(report, {"102"})
    assert {item["pid"] for item in scoped["findings"]} == {"102"} and scoped["zero_stores"] == []
    assert all(row["store"] == "Campx" for row in scoped["actions"]) and scoped["scoped"]


def test_non_usd_currency_skips_usd_thresholds():
    report = analysis.build_review({**REVIEW, "currency": "CAD"}, PEOPLE)
    assert report["findings"] == [] and any(fact["kind"] == "currency" for fact in report["facts"])


HOURLY = {"kind": "hourly", "day": "2026-11-23", "marketplace_time": "11-23 16:30", "currency": "USD", "errors": {},
          "roster": PROFILES, "profiles": PROFILES, "disabled": [], "missing": [],
          "campaigns": {"101": {"1": {"name": "A", "type": "sp", "state": "enabled", "t": m(3000, 40, 45, 0, 0)},
                                "2": {"name": "B", "type": "sp", "state": "enabled", "t": m(3000, 40, 60, 80, 1)},
                                "3": {"name": "C", "type": "sp", "state": "enabled", "t": m(900, 20, 12, 0, 0)},
                                "4": {"name": "D", "type": "sp", "state": "enabled", "t": m(500, 10, 8, 90, 3)},
                                "5": {"name": "E", "type": "sp", "state": "paused", "t": m(3000, 40, 45, 0, 0)},
                                "6": {"name": "F", "type": "sp", "state": "enabled", "t": m(3000, 40, 50, 300, 6)}},
                        "102": {"7": {"name": "G", "type": "sp", "state": "enabled", "t": m(2000, 10, 31, 0, 0)}}}}


def test_hourly_categories_and_once_per_day_deduplication():
    report = analysis.build_hourly(HOURLY, PEOPLE)
    found = {(item["pid"], item["cid"]): item["category"] for item in report["findings"]}
    assert found == {("101", "1"): "high_no_conv", ("101", "2"): "high_low_conv", ("101", "3"): "low_no_conv",
                     ("101", "4"): "low_high_conv", ("102", "7"): "high_no_conv"}
    assert [item["cid"] for item in report["grouped"]["high_no_conv"]] == ["1", "7"]  # by spend
    again = analysis.build_hourly(HOURLY, PEOPLE, alerted={"101:1:high_no_conv", "101:3:low_no_conv"})
    assert {item["cid"] for item in again["new"]} == {"2", "4", "7"}
    # A campaign that later crosses into a different category is new again.
    assert analysis.finding_key(again["new"][0]).count(":") == 2
    scoped = analysis.hourly_slice(report, {"102"})
    assert [item["cid"] for item in scoped["new"]] == ["7"]


def test_hourly_thresholds_are_owner_configurable():
    report = analysis.build_hourly(HOURLY, PEOPLE, {"high_spend": 100, "low_spend": 40, "min_clicks": 5})
    assert {item["cid"]: item["category"] for item in report["findings"]} == {
        "1": "low_no_conv", "4": "low_high_conv", "6": "low_high_conv"}
