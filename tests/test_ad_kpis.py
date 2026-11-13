from datetime import date, timedelta

import pytest

from jx_alert.analysis.ad_kpis import compare_kpis


DAY = date(2026, 11, 11)


def records():
    return {DAY - timedelta(days=i): {
        "spend": 100, "sales": 1000, "orders": 10, "units": 10,
        "clicks": 100, "impressions": 10000, "total_sales": 3000,
        "currency": "USD", "sales_currency": "USD", "total_sales_currency": "USD",
    } for i in range(22)}


def test_all_seven_ratios_use_the_confirmed_formulas_and_adverse_directions():
    data = records()
    data[DAY].update(spend=200, sales=500, orders=5, units=4, clicks=50)
    result = compare_kpis(data, DAY, store_scope=True)
    assert set(result) == {"CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS"}
    assert {key: item["current"] for key, item in result.items()} == {
        "CTR": 0.005, "CPC": 4, "CDR": 0.1, "CPS": 50, "ROAS": 2.5, "CPA": 40, "TCOAS": 200 / 3000,
    }
    assert result["CDR"]["status"] == "NON_ADVERSE"
    assert all(item["status"] == "ADVERSE" for key, item in result.items() if key != "CDR")


def test_baseline_uses_ratio_of_sums_and_falls_back_only_to_exact_adjacent_day():
    data = records()
    data[DAY - timedelta(days=1)].update(spend=300, clicks=10)
    entry = compare_kpis(data, DAY)["CPC"]
    assert entry["baseline"] == pytest.approx(900 / 610)
    del data[DAY - timedelta(days=3)]
    entry = compare_kpis(data, DAY)["CPC"]
    assert entry["comparison_basis"] == "PREVIOUS_DAY" and entry["baseline"] == 30
    del data[DAY - timedelta(days=1)]
    assert compare_kpis(data, DAY)["CPC"]["status"] == "NO_BASELINE"


def test_conversion_maturity_keeps_recent_declines_out_of_final_comparisons():
    data = records()
    data[DAY].update(orders=0, units=0, sales=0)
    result = compare_kpis(data, DAY, maturity_days=7)
    assert result["CPA"]["current_date"] == (DAY - timedelta(days=7)).isoformat()
    assert result["CPA"]["status"] == result["ROAS"]["status"] == "NON_ADVERSE"
    assert result["CPC"]["current_date"] == DAY.isoformat()


@pytest.mark.parametrize("problem,status", [
    ("zero", "ZERO_DENOMINATOR"), ("missing", "INSUFFICIENT_DATA"),
    ("currency", "CURRENCY_UNCONFIRMED"), ("small", "LOW_SAMPLE"),
])
def test_invalid_or_small_samples_remain_unknown(problem, status):
    data = records()
    if problem == "zero":
        data[DAY]["orders"] = 0
    elif problem == "missing":
        data[DAY].pop("orders")
    elif problem == "currency":
        data[DAY]["currency"] = None
    else:
        data[DAY]["clicks"] = 1
    assert compare_kpis(data, DAY)["CPA"]["status"] == status


def test_total_sales_ratio_requires_store_scope_and_matching_currency():
    data = records()
    assert compare_kpis(data, DAY)["TCOAS"]["status"] == "SCOPE_UNSUPPORTED"
    data[DAY]["total_sales_currency"] = "CAD"
    assert compare_kpis(data, DAY, store_scope=True)["TCOAS"]["status"] == "CURRENCY_UNCONFIRMED"


def test_changed_currency_never_labels_prior_money_as_current_currency():
    data = records()
    for offset in range(1, 8):
        data[DAY - timedelta(days=offset)]["currency"] = "CAD"
    result = compare_kpis(data, DAY)["CPC"]
    assert result["status"] == "CURRENCY_UNCONFIRMED"
    assert result["current"] == 1
    assert result["baseline"] is None
