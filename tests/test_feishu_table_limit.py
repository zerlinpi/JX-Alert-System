from copy import deepcopy

import pytest

from jx_alert.bot_service import _answer_cards
from jx_alert.notifications.cards import card
from jx_alert.notifications.store_health import card_fits


def _data(tables):
    return card("数据", subtitle="测试店铺", sections=[
        ("分区" + str(i), ["指标", "数值"], [("证据" + str(i), i)]) for i in range(tables)])


def _table_count(value):
    if isinstance(value, dict):
        return int(value.get("tag") == "table") + sum(_table_count(v) for v in value.values())
    if isinstance(value, list):
        return sum(_table_count(v) for v in value)
    return 0


@pytest.mark.parametrize("count,expected", [(5, True), (6, False)])
@pytest.mark.parametrize("nested", [False, True])
def test_small_cards_still_respect_five_tables_including_nested_components(count, expected, nested):
    value = _data(count)
    if nested:
        value["body"]["elements"] = [{"tag": "column_set", "columns": [
            {"tag": "column", "elements": value["body"]["elements"]}]}]
    assert card_fits(value) is expected


def _context(with_details):
    return {"store_name": "测试店铺", "report_date": "2026-11-14",
        "advertising_totals": [{"ad_type": "商品推广", "spend": 12, "orders": 1, "units": 1,
            "sales": 40, "clicks": 7, "impressions": 1000, "spend_currency": "USD", "sales_currency": "USD"}],
        "ad_detail_coverage": [{"dataset": "品牌与展示明细", "status": "采集完成", "rows": 1}] if with_details else []}


def test_answer_splits_six_tables_without_losing_facts_or_repeating_analysis():
    source = _data(4)
    before = deepcopy(source)
    result = _answer_cards(_context(True), source, "只列出有证据的问题。")
    assert [_table_count(c) for c in result] == [5, 1, 0]
    assert all(card_fits(c) for c in result) and source == before
    tables = [e for c in result for e in c["body"]["elements"] if e.get("tag") == "table"]
    assert [t["rows"][0]["c0"] for t in tables] == [
        "证据0", "证据1", "证据2", "证据3", "商品推广", "品牌与展示明细"]
    assert tables[4]["rows"][0]["c1"] == "美元 12.00"
    assert tables[5]["rows"][0]["c2"] == "1"


def test_answer_with_only_five_tables_does_not_add_an_unnecessary_page():
    result = _answer_cards(_context(False), _data(4), "")
    assert len(result) == 1 and _table_count(result[0]) == 5 and card_fits(result[0])
