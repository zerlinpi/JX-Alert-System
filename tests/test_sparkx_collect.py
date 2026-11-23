"""SparkX read-only client and lane collection against the documented envelope."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jx_alert.sparkx import client, collect
from jx_alert.sparkx.client import QueryRejected, SparkXError, envelope, validate_performance_query
from sparkx_fake import FakeSparkX, install, metrics

NOW = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)  # 08:30 Beijing; LA is still 11-23
DAY, BEFORE = "2026-11-22", "2026-11-21"
EMPTY = {"version": 1, "revision": "0" * 64, "profiles": {}}


def daily_rows():
    ads, business = [], []
    for pid, today, before, total in [
            (101, metrics(20000, 1500, 3217.70, 10726.72, 239, 256), metrics(18000, 1300, 2616.0, 9000, 200), 20000),
            (102, metrics(9000, 300, 598.32, 927.93, 7), metrics(8000, 260, 494.48, 900, 6), 1500),
            (103, metrics(4000, 120, 184.25, 0, 0), metrics(1500, 40, 61.2, 0, 0), 300),
            (104, metrics(800, 20, 33.48, 179.99, 1), metrics(700, 18, 31.0, 160, 1), 400)]:
        ads += [{"date": "20261122", "profileId": pid, **today}, {"date": "20261121", "profileId": pid, **before}]
        business += [{"date": "20261122", "profileId": pid, "TotalSalesAmount": total, "OrderCount": total / 50,
                      "UnitCount": total / 45},
                     {"date": "20261121", "profileId": pid, "TotalSalesAmount": total * 0.9, "OrderCount": total / 60,
                      "UnitCount": total / 55}]
    return {("campaign", BEFORE, DAY): ads, ("asin", BEFORE, DAY): business}


def test_daily_collection_uses_one_multi_profile_query_per_need(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(daily_rows()), collect)
    rosters = []
    data = collect.collect(settings, "daily", now=NOW, directory=EMPTY, on_roster=rosters.append)
    assert (data["report_date"], data["previous_date"], data["currency"]) == (DAY, BEFORE, "USD")
    assert set(data["profiles"]) == {"101", "102", "103", "104", "105", "201"} and rosters[0] == data["roster"]
    assert data["daily"]["101"][DAY]["spend"] == pytest.approx(3217.70)
    assert "105" not in data["daily"]  # no row stays missing, never zero
    assert data["business"]["101"][DAY]["total_sales"] == 20000
    tools = [(tool, args.get("factEntity")) for tool, args in fake.calls]
    assert tools == [("get_user_authorized_context", None), ("get_ads_perf", "campaign"), ("get_ads_perf", "asin")]
    assert all(len(args["profileIds"]) == 6 for tool, args in fake.calls if tool == "get_ads_perf")
    assert all(tool in client.READ_TOOLS for tool, _ in fake.calls)


def test_owner_disabled_profiles_are_never_queried_and_missing_ones_are_reported(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(daily_rows()), collect)
    directory = {"version": 1, "revision": "0" * 64, "profiles": {
        "105": {"monitored": False, "operators": []}, "999": {"monitored": True, "operators": []}}}
    data = collect.collect(settings, "daily", now=NOW, directory=directory)
    assert "105" not in data["profiles"] and data["disabled"] == ["105"] and data["missing"] == ["999"]
    assert all(105 not in args["profileIds"] for tool, args in fake.calls if tool == "get_ads_perf")


def test_business_failure_keeps_the_ad_report_and_records_the_gap(monkeypatch, settings):
    rows = daily_rows()
    install(monkeypatch, FakeSparkX(rows, errors={("asin", BEFORE, DAY): "scope_missing"}), collect)
    data = collect.collect(settings, "daily", now=NOW, directory=EMPTY)
    assert data["business"] is None and data["errors"] == {"business": "SPARKX_SCOPE_MISSING"}
    assert data["daily"]["102"][DAY]["orders"] == 7


@pytest.mark.parametrize("mutate,code", [
    (lambda rows: rows[("campaign", BEFORE, DAY)].append(dict(rows[("campaign", BEFORE, DAY)][0])),
     "SPARKX_DUPLICATE_ROW"),
    (lambda rows: rows[("campaign", BEFORE, DAY)][0].update(Clicks=999999), "SPARKX_TRAFFIC_CONTRADICTORY"),
    (lambda rows: rows[("campaign", BEFORE, DAY)][0].update(Spend="lots"), "SPARKX_METRIC_INVALID"),
    (lambda rows: rows[("campaign", BEFORE, DAY)][0].update(date="20261101"), "SPARKX_ROW_SCOPE_MISMATCH"),
])
def test_contradictory_or_out_of_scope_rows_fail_the_whole_lane(monkeypatch, settings, mutate, code):
    rows = daily_rows()
    mutate(rows)
    install(monkeypatch, FakeSparkX(rows), collect)
    with pytest.raises(SparkXError) as exc:
        collect.collect(settings, "daily", now=NOW, directory=EMPTY)
    assert exc.value.code == code


def test_truncation_hint_fails_but_the_shared_boundary_sentence_does_not(monkeypatch, settings):
    install(monkeypatch, FakeSparkX(daily_rows(), hint="Result truncated to the first 500 rows. "
                                    "Do not present partial or paginated results as complete."), collect)
    with pytest.raises(SparkXError) as exc:
        collect.collect(settings, "daily", now=NOW, directory=EMPTY)
    assert exc.value.code == "SPARKX_SOURCE_TRUNCATED"


def test_review_collects_two_windows_and_counts_enabled_campaigns_of_idle_stores(monkeypatch, settings):
    monkeypatch.setattr(collect, "METADATA_PAUSE", 0)
    select = {"campaignName": "Auto", "campaignType": "sponsoredProducts", "campaignState": "enabled"}
    rows = {("campaign", "2026-11-16", "2026-11-22"): [
                {"profileId": 101, "campaignId": 1, **select, **metrics(9000, 120, 162, 93, 2)}],
            ("campaign", "2026-11-09", "2026-11-15"): [
                {"profileId": 101, "campaignId": 1, **select, **metrics(8000, 110, 150, 484, 9)},
                {"profileId": 101, "campaignId": 2, **select, **metrics(5000, 90, 126, 400, 8)}]}
    fake = install(monkeypatch, FakeSparkX(rows, enabled={"105": 44}), collect)
    data = collect.collect(settings, "review", now=NOW, directory=EMPTY)
    assert data["window"] == ["2026-11-16", "2026-11-22"] and data["previous_window"] == ["2026-11-09", "2026-11-15"]
    first, second = data["campaigns"]["101"]["1"], data["campaigns"]["101"]["2"]
    assert first["w"]["spend"] == 162 and first["p"]["spend"] == 150 and second["w"] is None
    assert data["enabled_campaigns"]["105"] == 44 and data["enabled_campaigns"]["102"] == 0
    metadata = [args for tool, args in fake.calls if tool == "get_entity_metadata"]
    assert all(args["filters"] == {"campaignState": "enabled"} and len(args["profileIds"]) == 1 for args in metadata)


def test_hourly_reads_today_in_marketplace_time(monkeypatch, settings):
    rows = {("campaign", "2026-11-23", "2026-11-23"): [
        {"profileId": 102, "campaignId": 4, "campaignName": "TEST", "campaignState": "enabled", **metrics(3000, 95, 45, 0, 0)}]}
    install(monkeypatch, FakeSparkX(rows), collect)
    data = collect.collect(settings, "hourly", now=NOW, directory=EMPTY)
    assert data["day"] == "2026-11-23" and data["marketplace_time"] == "11-23 16:30"
    assert data["campaigns"]["102"]["4"]["t"]["spend"] == 45


def test_pagination_reads_every_page_and_never_returns_a_partial_result(monkeypatch, settings):
    many = [{"profileId": 101, "campaignId": index, "campaignName": f"c{index}", "campaignState": "enabled",
             **metrics(10, 1, 1, 0, 0)} for index in range(1, 1201)]
    install(monkeypatch, FakeSparkX({("campaign", "2026-11-23", "2026-11-23"): many}), collect)
    data = collect.collect(settings, "hourly", now=NOW, directory=EMPTY)
    assert len(data["campaigns"]["101"]) == 1200


def test_missing_token_and_no_profiles_are_bounded_codes(monkeypatch, settings):
    blank = settings.model_copy(update={"sparkx_ai_token": ""})
    with pytest.raises(SparkXError) as exc:
        collect.collect(blank, "daily", now=NOW, directory=EMPTY)
    assert exc.value.code == "SPARKX_NOT_CONFIGURED" and "Token" in exc.value.message
    install(monkeypatch, FakeSparkX({}, profiles={}), collect)
    with pytest.raises(SparkXError) as exc:
        collect.collect(settings, "daily", now=NOW, directory=EMPTY)
    assert exc.value.code == "SPARKX_NO_PROFILES"


def test_envelope_maps_documented_error_types_without_provider_text():
    failed = SimpleNamespace(isError=True, structuredContent={"errorType": "rate_limited", "message": "secret"}, content=[])
    with pytest.raises(SparkXError) as exc:
        envelope(failed, "get_ads_perf")
    assert exc.value.code == "SPARKX_RATE_LIMITED" and "secret" not in str(exc.value)
    text = SimpleNamespace(isError=False, structuredContent=None,
                           content=[SimpleNamespace(type="text", text='```json\n{"isError": false, "profiles": []}\n```')])
    assert envelope(text, "get_user_authorized_context") == {"isError": False, "profiles": []}
    with pytest.raises(SparkXError):
        envelope(SimpleNamespace(isError=False, structuredContent={"isError": False, "toolName": "other"}, content=[]),
                 "get_ads_perf")


def test_write_tools_are_unreachable():
    import anyio

    class Session:
        async def call_tool(self, tool, arguments):
            raise AssertionError("must not be called")

    for tool in ("batch_update_ads", "create_sp_sb_campaign", "delete_ai_managed_group"):
        with pytest.raises(SparkXError) as exc:
            anyio.run(client.call, Session(), tool, {})
        assert exc.value.code == "SPARKX_TOOL_NOT_ALLOWED"


def test_model_queries_are_bounded_to_monitored_profiles():
    base = {"profileIds": [101], "factEntity": "campaign", "dateStart": "2026-11-16", "dateEnd": "2026-11-22",
            "metrics": ["Spend", "ACOS"], "select": ["campaign.campaignName_"], "userContext": "问答"}
    validate_performance_query(base, {"101"})
    for change in ({"profileIds": [999]}, {"tenantId": 1}, {"factEntity": "keywordPlacement"},
                   {"timeGranularity": "hourly", "factEntity": "adGroup"}, {"dateStart": "2026-01-01"},
                   {"select": ["sum(p.cost) as x; drop"]}, {"metrics": ["spend; drop"]},
                   {"filters": {"Spend": None}}, {"pageSize": 1000}):
        with pytest.raises(QueryRejected):
            validate_performance_query({**base, **change}, {"101"})
