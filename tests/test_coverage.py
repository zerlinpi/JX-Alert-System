"""The daily report's stores without data are named by the system from SparkX's rows, not by the model."""
from datetime import date
import json

from jx_alert.reports import coverage

DAY = date(2026, 11, 22)
NAMES = {"101": "香港川页", "102": "汤宝", "103": "陈子俊", "104": "OCTORA", "105": "昌泉"}
MONITORED = set(NAMES)
ADS = [  # one row per store and day; OCTORA only advertised the day before, 昌泉 not at all
    {"profileId": 101, "date": "20261122", "Spend": 3378.4, "Sales": 12295.57},
    {"profileId": 101, "date": "20261121", "Spend": 3466.1, "Sales": 11000.0},
    {"profileId": 102, "date": "20261122", "Spend": 560.09, "Sales": 1475.69},
    {"profileId": 103, "date": "20261122", "Spend": 0, "Sales": 0},
    {"profileId": 104, "date": "20261121", "Spend": 47.8, "Sales": 0}]
TOTALS = [  # 汤宝: ad sales but no store sales, i.e. no store totals; 香港川页: units but no orders yet
    {"profileId": 101, "date": "20261122", "TotalSalesAmount": 21952.73, "OrderCount": 0, "UnitCount": 525},
    {"profileId": 102, "date": "20261122", "TotalSalesAmount": 0, "OrderCount": 0, "UnitCount": 0},
    {"profileId": 103, "date": "20261122", "TotalSalesAmount": 229.98, "OrderCount": 0, "UnitCount": 2}]


def answer(rows, *, more=False):
    meta = {"page": 1, "pageSize": 500, "hasNextPage": more, "hint": "Missing fields are not zero."}
    return json.dumps({"isError": False, "toolName": "get_ads_perf", "data": {"rows": rows, "rowCount": len(rows)},
                       "meta": meta})


def query(entity, ids=MONITORED, *, start="2026-11-21", end="2026-11-22", **extra):
    return {"profileIds": sorted(int(pid) for pid in ids), "factEntity": entity, "dateStart": start,
            "dateEnd": end, "select": ["profile.profileId_", "date"], **extra}


def summary(*calls):
    """Agent summary for (arguments, rows) or (arguments, rows, has_next_page) SparkX calls."""
    return {"tools_used": ["get_ads_perf"] * len(calls), "requests": [call[0] for call in calls],
            "evidence": [answer(call[1], more=call[2] if len(call) > 2 else False) for call in calls]}


def test_stores_are_sorted_by_what_sparkx_returned_for_the_day():
    found = coverage.classify(summary((query("campaign"), ADS), (query("asin"), TOTALS)), monitored=MONITORED, day=DAY)
    assert found["spent"] == {"101", "102"} and found["idle"] == {"103"} and found["missing"] == {"104", "105"}
    assert found["totals_missing"] == {"102"} and found["orders_lagging"] == {"101"}
    assert coverage.note(found, NAMES) == ("暂无广告数据：OCTORA、昌泉；当日无广告花费：陈子俊；"
                                           "暂无店铺总数据：汤宝；总订单尚未更新：香港川页（表中记「—」）。")
    every = {**found, "orders_lagging": {"101", "102"}, "orders_counted": {"101", "102"}}
    assert "各店总订单尚未更新（表中记「—」）" in coverage.note(every, NAMES)
    assert coverage.note({**found, "missing": set(), "idle": set(), "totals_missing": set(),
                          "orders_lagging": set()}, NAMES) == ""


def test_a_single_day_query_and_totals_over_the_window_settle_stores_too():
    day_only = [row for row in ADS if row["date"] == "20261122"]
    window = [{"profileId": 101, "Spend": 6844.5}, {"profileId": 102, "Spend": 560.09}, {"profileId": 103, "Spend": 0}]
    found = coverage.classify(summary((query("campaign", start="2026-11-22"), day_only),
                                      (query("campaign", select=["profile.profileId_"]), window)),
                              monitored=MONITORED, day=DAY)
    # 104 and 105 had no row on the day; the window totals add no claim about the day itself.
    assert found["missing"] == {"104", "105"} and found["spent"] == {"101", "102"}


def test_rows_that_cannot_settle_every_store_leave_the_report_alone():
    unsettled = [
        summary((query("campaign"), ADS, True)),                                    # more pages not read
        summary((query("campaign", filters={"Spend": {">": 0}}), ADS)),             # filtered rows
        summary((query("campaign", ids={"101", "102", "103"}), ADS)),               # 104, 105 not queried
        summary((query("campaign", select=["profile.profileId_"]), [{"profileId": 101, "Spend": 1}, {
            "profileId": 102, "Spend": 2}, {"profileId": 103, "Spend": 0}, {"profileId": 104, "Spend": 47.8}])),
        summary((query("campaign", select=["date"]), [{"date": "20261122", "Spend": 3938.49}])),  # no store rows
        summary((query("campaign", start="2026-11-20", end="2026-11-21"), ADS)),    # another window
        summary((query("asin"), TOTALS)),                                           # no ad rows at all
        {"tools_used": ["get_ads_perf"], "requests": [query("campaign")], "evidence": ["not json"]},
    ]
    for value in unsettled:
        assert coverage.classify(value, monitored=MONITORED, day=DAY) is None
    # An empty answer of a complete, unfiltered query does settle it: no store had a row.
    nothing = coverage.classify(summary((query("campaign"), [])), monitored=MONITORED, day=DAY)
    assert nothing["missing"] == MONITORED and not nothing["spent"]


REPORT = """对比日 2026-11-21，监控 5 家店铺，当日有广告花费 2 家；金额均为 USD，广告订单、广告销售额等广告转化数据为暂定值。
**全公司核心指标**
| 指标 | 当日 | 前一日 | 环比 |
|---|---|---|---|
| 广告花费 | $3,938.49 | $3,513.90 | +12.1% |
**有花费店铺明细**
| 店铺 | 广告花费 | 广告销售额 | 总销售额 | 广告订单 | 总订单 | 运营 |
|---|---|---|---|---|---|---|
| 香港川页 | $3,378.40 | $12,295.57 | $21,952.73 | 269 | — | 未映射运营 |
| 汤宝 | $560.09 | $1,475.69 | — | 29 | — | 姚文婷 |
当日无广告花费：陈子俊、OCTORA。
**须知**
1. 花费高度集中：香港川页占 85.8%。
2. ACOS 明显偏高：汤宝 37.9%；汤宝总销售额暂无数据。
3. 昌泉、OCTORA 暂无数据；汤宝总销售额及各店总订单亦暂无数据。
4. 有广告花费但无广告订单的店铺：无。"""


def test_the_system_line_replaces_the_models_list_and_its_missing_data_remarks():
    line = "暂无广告数据：OCTORA、昌泉；当日无广告花费：陈子俊。"
    text = coverage.apply(REPORT, line)
    lines = text.split("\n")
    table_end = lines.index("| 汤宝 | $560.09 | $1,475.69 | — | 29 | — | 姚文婷 |")
    assert lines[table_end + 1] == line and lines[table_end + 2] == ""
    assert "当日无广告花费：陈子俊、OCTORA。" not in text
    assert text.endswith("**须知**\n1. 花费高度集中：香港川页占 85.8%。\n2. ACOS 明显偏高：汤宝 37.9%。\n"
                         "3. 有广告花费但无广告订单的店铺：无。")
    assert text.startswith("对比日 2026-11-21") and "| 广告花费 | $3,938.49 |" in text  # the rest is untouched


def test_the_line_has_a_place_without_a_store_table():
    text = coverage.apply("开头一句。\n\n**须知**\n1. 昌泉暂无数据。", "暂无广告数据：昌泉。")
    assert text == "开头一句。\n\n暂无广告数据：昌泉。"  # 须知 held only a missing-data remark
    assert coverage.apply("只有一句。", "暂无广告数据：昌泉。") == "只有一句。\n\n暂无广告数据：昌泉。"
    plain = coverage.apply("**有花费店铺明细**\n当日没有店铺有广告花费。\n当日无广告花费：全部。", "暂无广告数据：昌泉。")
    assert plain == "**有花费店铺明细**\n暂无广告数据：昌泉。\n\n当日没有店铺有广告花费。"
