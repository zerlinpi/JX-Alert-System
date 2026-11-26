"""The daily report's statements about stores come from SparkX's rows; sentences without information go."""
from datetime import date
import json

from jx_alert.reports import coverage

DAY, BEFORE = date(2026, 11, 22), date(2026, 11, 21)
NAMES = {"101": "香港川页", "102": "汤宝", "103": "陈子俊", "104": "OCTORA", "105": "昌泉"}
MONITORED = set(NAMES)
ADS = [  # one row per store and day: OCTORA and 陈子俊 spent the day before only, 昌泉 not at all
    {"profileId": 101, "date": "20261122", "Spend": 3378.4, "Sales": 12295.57},
    {"profileId": 101, "date": "20261121", "Spend": 3466.1, "Sales": 11000.0},
    {"profileId": 102, "date": "20261122", "Spend": 560.09, "Sales": 1475.69},
    {"profileId": 103, "date": "20261122", "Spend": 0, "Sales": 0},
    {"profileId": 103, "date": "20261121", "Spend": 12.0, "Sales": 0},
    {"profileId": 104, "date": "20261121", "Spend": 47.8, "Sales": 0}]


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


def stopped(*calls):
    return coverage.stopped(summary(*calls), monitored=MONITORED, day=DAY, previous=BEFORE)


def test_only_stores_that_stopped_spending_are_named():
    """Owner (Nov 26 2026): the daily list of dormant stores and data gaps says nothing; a stop does."""
    found = stopped((query("campaign"), ADS))
    # 陈子俊 returned a zero-spend row, OCTORA no row at all; 昌泉 spent neither day, so it is not named.
    assert found == {"104": 47.8, "103": 12.0}
    assert coverage.note(found, NAMES) == "当日无广告花费（前一日有）：OCTORA（前一日 $47.80）、陈子俊（前一日 $12.00）。"
    assert coverage.note({}, NAMES) == ""


def test_a_store_is_named_only_when_the_rows_settle_both_days():
    day_only = [row for row in ADS if row["date"] == "20261122"]
    window = [{"profileId": 101, "Spend": 6844.5}, {"profileId": 104, "Spend": 47.8}]
    unsettled = [
        [(query("campaign"), ADS, True)],                                      # more pages not read
        [(query("campaign", filters={"Spend": {">": 0}}), ADS)],               # filtered rows
        [(query("campaign", ids={"101", "102", "103"}), ADS)],                 # OCTORA not queried that day
        [(query("campaign", start="2026-11-22"), day_only)],                   # nothing about the day before
        [(query("campaign", select=["profile.profileId_"]), window)],         # totals without a date
        [(query("asin"), ADS)],                                                # no ad rows
    ]
    for calls in unsettled:
        assert "104" not in stopped(*calls), calls
    # The day before from one query, the day itself from another: both settled, so OCTORA is named.
    before = [row for row in ADS if row["date"] == "20261121"]
    assert stopped((query("campaign", start="2026-11-21", end="2026-11-21"), before),
                   (query("campaign", start="2026-11-22"), day_only)) == {"104": 47.8, "103": 12.0}


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
暂无店铺总数据：汤宝（表中记「—」）。
**须知**
1. 花费高度集中：香港川页占 85.8%。
2. ACOS 明显偏高：汤宝 37.9%；汤宝总销售额暂无数据。
3. 昌泉、OCTORA 暂无数据；汤宝总销售额及各店总订单亦暂无数据。
4. 有广告花费但无广告订单的店铺：无。
5. 金额均为 USD，近两天广告订单仍会回补，属暂定值。"""


def test_the_system_line_replaces_everything_written_under_the_store_table():
    line = "当日无广告花费（前一日有）：OCTORA（前一日 $47.80）。"
    text = coverage.apply(REPORT, line)
    lines = text.split("\n")
    table_end = lines.index("| 汤宝 | $560.09 | $1,475.69 | — | 29 | — | 姚文婷 |")
    assert lines[table_end + 1] == line and lines[table_end + 2] == ""
    assert "当日无广告花费：陈子俊" not in text and "暂无店铺总数据" not in text
    # 须知 keeps only sentences with information: no data remarks, reader notes or empty findings.
    assert text.endswith("**须知**\n1. 花费高度集中：香港川页占 85.8%。\n2. ACOS 明显偏高：汤宝 37.9%。")
    assert text.startswith("对比日 2026-11-21") and "| 广告花费 | $3,938.49 |" in text  # the rest is untouched
    assert coverage.apply(REPORT, "").count("\n| 汤宝 |") == 1 and "当日无广告花费" not in coverage.apply(REPORT, "")


def test_the_overview_before_the_first_section_goes():
    """Owner (Nov 26 2026): 「对比 …，监控 19 家店铺、当日有广告花费 13 家；金额已统一折算为 USD…」 is meaningless."""
    assert coverage.without_preamble(REPORT).startswith("**全公司核心指标**\n| 指标 | 当日 |")
    assert coverage.without_preamble("**须知**\n1. 花费集中。") == "**须知**\n1. 花费集中。"
    assert coverage.without_preamble("只有一段话，没有任何标题。") == "只有一段话，没有任何标题。"
    assert coverage.without_preamble("开场白。\n| 店铺 | 广告花费 |\n|---|---|") == "| 店铺 | 广告花费 |\n|---|---|"


def test_the_line_has_a_place_without_a_store_table():
    line = "当日无广告花费（前一日有）：昌泉（前一日 $5.00）。"
    text = coverage.apply("开头一句。\n\n**须知**\n1. 昌泉暂无数据。", line)
    assert text == "开头一句。\n\n" + line  # 须知 held only a missing-data remark
    assert coverage.apply("只有一句。", line) == "只有一句。\n\n" + line
    # Under a heading without a table, a sentence with information stays; only the model's store list goes.
    plain = coverage.apply("**有花费店铺明细**\n当日没有店铺有广告花费。\n当日无广告花费：全部。", line)
    assert plain == "**有花费店铺明细**\n" + line + "\n\n当日没有店铺有广告花费。"
