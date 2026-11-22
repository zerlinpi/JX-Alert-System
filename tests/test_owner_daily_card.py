from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from jx_alert.notifications.owner_daily_card import build_owner_daily_cards, build_owner_daily_pages
from jx_alert.notifications.store_health import card_fits


DAY = date(2026, 11, 25)
ADS = ["sp", "sb", "sbv", "sd", "共享分摊"]


def _store(sid, **overrides):
    return {"store_id": str(sid), "store_name": f"店铺{sid}", "currency": "USD",
            "covered_ad_types": ADS, "ad_spend": 100, "sales": 1000,
            "return_units": 2, "sold_units": 2, "refund_amount": 20, **overrides}


def _tables(value):
    return [element for element in value["body"]["elements"] if element["tag"] == "table"]


def _rows(table):
    return [tuple(row[column["name"]] for column in table["columns"]) for row in table["rows"]]


def test_summary_is_currency_separated_and_incomplete_ad_never_enters_full_total():
    rows = [
        _store(1),
        _store(2, ad_spend=1234, covered_ad_types=["campaign_sp"], ad_partial_spend=40, sales=0,
               sold_units=8, refund_amount=None),
        _store(3, currency="EUR", ad_spend=0, sales=Decimal("200.15"), return_units=None,
               sold_units=10, refund_amount=0),
        _store(4, currency=None, return_units=None, sold_units=None),
    ]
    summary, detail = build_owner_daily_cards(rows, report_date=DAY)
    assert _rows(_tables(summary)[0]) == [
        ("广告花费", "EUR 0.00", "1/4 店"),
        ("广告花费", "USD 100.00", "1/4 店"),
        ("净销售额", "EUR 200.15", "1/4 店"),
        ("净销售额", "USD 1,000.00", "2/4 店"),
        ("退款金额", "EUR 0.00", "1/4 店"),
        ("退款金额", "USD 20.00", "1/4 店"),
        ("已匹配退货 / 销量", "4 / 10 件", "2/4 店"),
        ("合计件数退货率", "40.00%", "2/4 店"),
    ]
    advertising, refunds = map(_rows, _tables(detail))
    assert advertising[1][1:] == ("SP 部分\nUSD 40.00", "USD 0.00")
    assert advertising[3][1:] == ("币种待确认", "币种待确认")
    assert refunds[1][1:] == ("2 / 8", "25.00%", "待核实")
    assert refunds[2][1:] == ("— / 10", "待核实", "EUR 0.00")
    assert "已发放结算口径" in json.dumps(summary, ensure_ascii=False)


def test_same_window_returns_can_exceed_sales_but_no_division_by_zero():
    rows = [_store(1, return_units=4, sold_units=0), _store(2, return_units=1, sold_units=2)]
    summary, detail = build_owner_daily_cards(rows, report_date=DAY)
    assert _rows(_tables(summary)[0])[-1] == ("合计件数退货率", "250.00%", "2/2 店")
    assert _rows(_tables(detail)[1])[0][1:3] == ("4 / 0", "待核实")
    only_summary, _ = build_owner_daily_cards(rows[:1], report_date=DAY)
    assert _rows(_tables(only_summary)[0])[-1] == ("合计件数退货率", "待核实", "1/1 店")


def test_native_mobile_table_styles_page_labels_and_per_page_scope():
    store_name = "超长店铺名称" * 12 + " <at id=ou_untrusted></at>"
    rows = [_store(index, store_name=store_name, ad_spend=index) for index in range(1, 15)]
    observed = datetime(2026, 11, 25, 12, 14, tzinfo=timezone.utc)
    pages = build_owner_daily_pages(rows, report_date=DAY, as_of=observed, provisional=True)
    expected = [str(index) for index in range(1, 15)]
    assert [ids for _, ids in pages] == [expected, expected[:6], expected[6:12], expected[12:]]
    for index, (page, _) in enumerate(pages):
        assert card_fits(page)
        subtitle = page["body"]["elements"][0]["content"]
        assert "店铺当地报表日 2026-11-25（未结束，仅供参考）" in subtitle
        assert "生成于 2026-11-25T12:14:00+00:00" in subtitle
        assert ("汇总 第 1/1 页" if not index else f"明细 第 {index}/3 页") in subtitle
        for table in _tables(page):
            cols = table["columns"]
            assert 1 <= table["page_size"] <= 10
            assert len(cols) <= 4 and table["freeze_first_column"] is True
            assert cols[0]["horizontal_align"] == "left"
            assert all(c["data_type"] == "text" for c in cols)
            assert all(c["horizontal_align"] == "right" for c in cols[1:])
            assert sum(int(c["width"][:-1]) for c in cols) == 100


def test_empty_and_invalid_inputs_do_not_create_fake_amounts_or_duplicate_stores():
    (summary, scope), = build_owner_daily_pages([], report_date=DAY)
    assert scope == []
    assert _rows(_tables(summary)[0])[-1] == ("合计件数退货率", "待核实", "0/0 店")
    with pytest.raises(ValueError, match="unique"):
        build_owner_daily_cards([_store(1), _store("1")], report_date=DAY)
    with pytest.raises(ValueError, match="timezone"):
        build_owner_daily_cards([], report_date=DAY, as_of=datetime(2026, 11, 25))
    with pytest.raises(ValueError, match="timestamp"):
        build_owner_daily_cards([], report_date=DAY, as_of="<at id=ou_untrusted></at>")
