"""Feishu card layout of the daily report, the 7-day review and the hourly monitor."""
import copy

from jx_alert.notifications.cards import card_fits, plain_text
from jx_alert.reports import cards
from jx_alert.sparkx import analysis
from test_sparkx_analysis import DAILY, HOURLY, PEOPLE, REVIEW, m


def tables(value):
    return sum(element["tag"] == "table" for element in value["body"]["elements"])


def markdown_text(value):
    return "\n".join(element["content"] for element in value["body"]["elements"] if element["tag"] == "markdown")


def test_daily_card_has_company_kpis_site_and_store_tables():
    report = analysis.build_daily(DAILY, PEOPLE)
    notes = [{"title": "加拿大没有数据", "text": "先核对 Token 授权范围。"}, {"title": "", "text": "Campx ACOS 64.5%。"}]
    value = cards.daily_card(report, notes, "deepseek")
    assert card_fits(value) and tables(value) == 3
    text = plain_text(value)
    assert text.startswith("SparkX 广告日报 · 2026-11-22")
    assert "统计日 2026-11-22（美西时间，已结束的完整日），环比 2026-11-21。监控店铺 7 个，SparkX 有数据 5 个，当日有花费 4 个" in text
    assert "店铺总销售额 | $22,450 | $20,060 | +11.9%" in text
    assert "总订单量 | 449 | 390 | +15.1%" in text
    assert "广告花费 | $4,034 | $3,203 | +25.9%" in text
    assert "ACOS | 34.1% | 31.8% | +2.2pp" in text
    assert "PPC（花费/点击） | $2.08 |" in text and "TACOS | 18.2% |" in text
    assert "环比口径：广告数据当日 5 家、前一日 4 家；店铺总销售当日 5 家、前一日 4 家有数据" in text
    assert "美国 | USD | 6 | 4 | 4,034 | 11,835 | 264 | 247 | 34.1% | 2.93 | +25.9% | +17.6%" in text
    assert "加拿大 | USD | 1 | 0 | — | — | — | — | — | — | — | —" in text
    assert "合计（USD） | USD | 7 | 4 |" in text
    assert "美国 4 个有花费店铺明细" in text
    assert "香港川页 | 皮泽霖 | 3,217.70 | 10,726.72 | 256 | 239 | 30.0% | 16.1% | +23%" in text
    assert "FITFIZZ | 未映射运营 | 184.25 | 0.00 | 0 | 0 | — | 61.4% | +201%" in text
    assert "美国当日无花费：昌泉" in text and "美国 SparkX 无记录：迈源" in text
    assert "两点须知" in text and "1. **加拿大没有数据**：先核对 Token 授权范围。" in markdown_text(value)
    assert "数字已逐项核对" in text


def test_review_card_follows_the_four_part_structure():
    report = analysis.build_review(REVIEW, PEOPLE)
    value = cards.review_card(report, {"headline": None, "actions": report["actions"]}, "rules")
    assert card_fits(value) and tables(value) == 5
    text = plain_text(value)
    assert "7 个店铺中，4 个有投放数据，3 个检出 5 个异常 campaign。" in text
    assert "统计范围：2026-11-16~2026-11-22（近 7 日）对比 2026-11-09~2026-11-15（前 7 日），campaign 级。" in text
    assert "一、最严重" in text and "昌泉 全店零曝光" in text and "44 个 campaign 处于开启状态" in text
    assert "二、异常店铺总览" in text and "三、按问题类型看具体 campaign" in text and "四、建议动作" in text
    assert "① 高花费零出单" in text and "② ACOS 明显恶化" in text and "③ 加预算后销售反而下滑" in text
    assert "④ 异常停摆" in text
    assert "香港川页 | 皮泽霖 | 14MQ-S-SP-queen bed platform | 31% → 174% | $162 | $93.00 | 1.35 → 0.24" in text
    assert "Campx | 高爽 | CTH-ASIN定投-TEST | $223 | 95 | 2.35 → 1.64" in text
    assert "香港川页 | 皮泽霖 | S-14MQ-metal bed frame | $126 | 无投放记录" in text
    assert "另有 2 个店铺两周零数据" in text


def test_low_spend_high_conversion_campaigns_are_listed_as_opportunities():
    data = copy.deepcopy(REVIEW)
    data["campaigns"]["104"]["8"] = {"name": "陈子俊-Exact", "type": "sponsoredProducts", "state": "enabled",
                                     "w": m(800, 30, 40, 400, 5), "p": m(700, 25, 35, 300, 4)}
    report = analysis.build_review(data, PEOPLE)
    assert [item["cid"] for item in report["opportunities"]] == ["8"]
    assert any(fact["text"] == "低花费高转化 1 个" for fact in report["facts"])
    value = cards.review_card(report, {"headline": None, "actions": report["actions"]}, "rules")
    assert card_fits(value) and tables(value) == 5
    assert "**⑤ 低花费高转化（机会，可加预算或出价放量）**\n- 陈子俊（未映射运营）陈子俊-Exact：近 7 日花费 $40.00、5 单、ACOS 10.0%" \
        in plain_text(value)
    assert analysis.review_slice(report, {"102"})["opportunities"] == []


def test_operator_review_card_only_holds_their_stores_and_none_when_clean():
    report = analysis.build_review(REVIEW, PEOPLE)
    scoped = analysis.review_slice(report, {"102"})
    value = cards.review_card(scoped, {"headline": None, "actions": scoped["actions"]}, "rules")
    text = plain_text(value)
    assert "你负责的店铺中，1 个检出 2 个异常 campaign。" in text
    assert "香港川页" not in text and "昌泉" not in text
    assert cards.review_card(analysis.review_slice(report, {"104"}), {"actions": []}, "rules") is None


def test_untrusted_names_cannot_inject_links_or_mentions():
    report = analysis.build_review(REVIEW, PEOPLE)
    value = cards.review_card(report, {"headline": None, "actions": report["actions"]}, "rules")
    markdown = markdown_text(value)
    assert "<at" not in markdown and "](" not in markdown and "https://" not in markdown
    hourly = analysis.build_hourly({**HOURLY, "campaigns": {"101": {"1": {
        "name": "<at id=ou_x></at>[a](https://x)", "type": "sp", "state": "enabled",
        "t": HOURLY["campaigns"]["101"]["1"]["t"]}}}}, PEOPLE)
    hourly_value = cards.hourly_card(hourly)
    assert "<at" not in markdown_text(hourly_value)


def test_hourly_card_groups_new_findings_by_category():
    report = analysis.build_hourly(HOURLY, PEOPLE)
    value = cards.hourly_card(report)
    assert card_fits(value) and tables(value) == 4 and value["header"]["template"] == "red"
    text = plain_text(value)
    assert text.startswith("广告实时监控 · 美西 2026-11-23 16:30")
    assert "美西 2026-11-23 今日截至 11-23 16:30" in text and "2 个店铺 5 个广告活动触发提醒" in text
    assert "高花费无转化（2）" in text and "低花费高转化（1）" in text
    assert "香港川页 | 皮泽霖 | A | $45.00 | 40 | 0 | — | 0.0% | $1.12" in text
    assert "同一活动同类提醒每天只发一次" in text
    assert cards.hourly_card(analysis.build_hourly(HOURLY, PEOPLE, alerted={
        analysis.finding_key(item) for item in report["findings"]})) is None
