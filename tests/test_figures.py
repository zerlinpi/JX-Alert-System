"""The daily report's 环比 column must agree with the 当日 and 前一日 values of its own row."""
from jx_alert.reports import figures

# The company table of a live flash run (Nov 2026): every change adds up except 广告花费.
LIVE = """对比日 2026-11-22：19 家店铺中 13 家有广告花费，金额均为 USD。
**全公司核心指标**
| 指标 | 当日 | 前一日 | 环比 |
|---|---|---|---|
| 总销售额 | $40,419.54 | $33,385.83 | +21.1% |
| 广告销售额 | $21,543.86 | $20,822.02 | +3.5% |
| 总订单 | — | 596 | — |
| 广告订单 | 357 | 372 | -4.0% |
| 总销量 | 712 | 623 | +14.3% |
| 广告销量 | 393 | 394 | -0.3% |
| 广告花费 | $6,072.34 | $7,093.46 | -22.7% |
| ACOS | 28.2% | 34.1% | -5.9 pt |
| TACOS | 13.6% | 19.7% | -6.0 pt |
| CVR | 8.9% | 8.4% | +0.6 pt |
| PPC（CPC） | $1.52 | $1.59 | -4.5% |
| ROAS | 3.55 | 2.94 | +0.61 |
**须知**
1. 全公司花费降 22.7%、广告销售额增 3.5%，ACOS 由 34.1% 降至 28.2%，效率改善。"""


def test_a_change_that_does_not_add_up_is_corrected_in_the_table_and_the_text():
    text, corrections = figures.check_changes(LIVE)
    assert corrections == 1
    assert "| 广告花费 | $6,072.34 | $7,093.46 | -14.4% |" in text
    assert "1. 全公司花费降 14.4%、广告销售额增 3.5%" in text
    # Every other line is exactly as written.
    assert [line for line in text.split("\n") if "广告花费" not in line and "全公司花费" not in line] == [
        line for line in LIVE.split("\n") if "广告花费" not in line and "全公司花费" not in line]


def test_rounding_points_and_other_ways_of_writing_a_change_are_accepted():
    rows = ["| ACOS | 28.4% | 34.1% | -5.7 个百分点 |",   # points
            "| ACOS | 28.4% | 34.1% | -5.7% |",          # points written with %
            "| ACOS | 28.4% | 34.1% | -16.7% |",         # relative change of the ratio
            "| ROAS | 3.52 | 2.94 | +20.0% |",            # rounding of both figures
            "| PPC（CPC） | $1.52 | $1.59 | -$0.07 |",    # absolute change in dollars
            "| 广告订单 | 3 | 0 | +300% |",               # no relative change from zero: left alone
            "| 广告花费 | $6,072.34 | $7,093.46 | 下降 22.7% |",  # not a plain number: left alone
            "| 总订单 | — | 596 | — |"]
    table = "| 指标 | 当日 | 前一日 | 环比 |\n|---|---|---|---|\n" + "\n".join(rows)
    assert figures.check_changes(table) == (table, 0)


def test_the_store_table_is_led_by_the_figures_the_owner_reads_first():
    """Owner (Nov 26 2026): 广告花费、ACOS、TACOS、CPC、ROAS、CVR right after 店铺, whatever order the model wrote."""
    written = ("| 指标 | 当日 | 前一日 | 环比 |\n|---|---|---|---|\n| ACOS | 27.9% | 25.3% | +2.6pp |\n\n"
               "**有花费店铺明细**\n"
               "| **店铺** | 广告花费 | 广告销售额 | 总销售额 | ACOS | TACOS | 花费日环比 | 运营 | PPC（CPC） | ROAS | CVR |\n"
               "|:---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|\n"
               "| 香港川页 | $2,990.52 | $12,235.71 | $23,932.80 | 24.4% | 12.5% | -11.6% | 未映射运营 | $1.33 | 4.09 | 11.0% |\n"
               "| 汤宝 | $408.72 | $1,092.77 | — | 37.4% | — | -27.0% | 姚文婷 |\n\n**须知**\n1. 花费集中。")
    text = figures.arrange_store_table(written)
    lines = text.split("\n")
    assert lines[:5] == written.split("\n")[:5]  # the company table is not touched
    assert lines[5] == ("| **店铺** | 广告花费 | ACOS | TACOS | PPC（CPC） | ROAS | CVR | 广告销售额 | 总销售额 | 花费日环比 "
                        "| 运营 |")
    assert lines[6] == "|---|---|---|---|---|---|---|---|---|---|---|"
    assert lines[7] == ("| 香港川页 | $2,990.52 | 24.4% | 12.5% | $1.33 | 4.09 | 11.0% | $12,235.71 | $23,932.80 "
                        "| -11.6% | 未映射运营 |")
    assert lines[8] == "| 汤宝 | $408.72 | 37.4% | — | — | — | — | $1,092.77 | — | -27.0% | 姚文婷 |"  # short row
    assert lines[9:] == ["", "**须知**", "1. 花费集中。"]
    assert figures.arrange_store_table(text) == text  # already in order: unchanged
    site = "| 站点 | 总销售额 | 广告花费 |\n|---|---|---|\n| 美国 | $1 | $2 |"
    assert figures.arrange_store_table(site) == site  # only the store table is arranged


def test_a_wrong_number_that_is_also_a_table_figure_is_left_in_the_text():
    text = ("| 指标 | 当日 | 前一日 | 环比 |\n|---|---|---|---|\n| 广告花费 | $6,072.34 | $7,093.46 | -22.7% |\n\n"
            "| 店铺 | ACOS |\n|---|---|\n| Campx | 22.7% |\n\n花费降 22.7%。")
    fixed, corrections = figures.check_changes(text)
    assert corrections == 1 and "| 广告花费 | $6,072.34 | $7,093.46 | -14.4% |" in fixed
    assert fixed.endswith("花费降 22.7%。") and "| Campx | 22.7% |" in fixed  # ambiguous: not touched
    wrong_way = "| 指标 | 当日 | 前一日 | 环比 |\n|---|---|---|---|\n| 广告花费 | $6,072.34 | $7,093.46 | +14.4% |\n上升 14.4%"
    fixed, corrections = figures.check_changes(wrong_way)
    assert corrections == 1 and "| -14.4% |" in fixed and fixed.endswith("上升 14.4%")  # direction: table only
