"""Exact provider boundaries, no live requests."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import anyio
from mcp.types import CallToolResult, TextContent
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert import sparkx_backend as sparkx
from jx_alert.config import Settings
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, RawSnapshot, Store


PROFILE = 4404871489220462
NOW = datetime(2026, 11, 24, 18, tzinfo=timezone.utc)
BINDING = dict(profile_id=PROFILE, marketplace="ATVPDKIKX0DER", timezone="America/Los_Angeles", currency="USD")
CHOICE = dict(source="sparkx", binding=BINDING, selected_at="2026-11-24T00:00:00+00:00",
              verification={"verified_at": "2026-11-23T00:00:00+00:00"})


def _settings():
    return Settings(_env_file=None, sparkx_ai_token="test-only", sparkx_profile_ids=str(PROFILE))


def _store(**overrides):
    return SimpleNamespace(**{**dict(sid="5174", status=1, marketplace_id="ATVPDKIKX0DER",
        country="US", timezone="America/Los_Angeles", currency_code="USD"), **overrides})


def _ok(tool, **values):
    return dict(isError=False, toolName=tool, **values)


def _result(envelope, text=None):
    return CallToolResult(structuredContent=envelope, content=[TextContent(type="text", text=text)] if text else [])


@pytest.fixture
def source(monkeypatch):
    wire = {"calls": [], "failure": None}
    monkeypatch.setattr(sparkx, "_PAGE_SIZE", 2)

    async def receive(tool, args, *, token, timeout_seconds):
        assert token == "test-only" and timeout_seconds > 0
        wire["calls"].append((tool, args))
        failure = wire["failure"]
        if tool == "get_user_authorized_context":
            scopes = [dict(profileId=PROFILE, countryCode="US", storeType="seller")]
            if failure == "other_market":
                scopes[0]["countryCode"] = "CA"
            if failure == "missing_type":
                scopes[0].pop("storeType")
                scopes[0]["profileName"] = "Sample verified store"
            if failure == "unknown_declared_type":
                scopes[0]["storeType"] = "unknown"
            if failure == "null_declared_type":
                scopes[0]["storeType"] = None
            if failure == "conflicting_roster":
                scopes += [dict(profileId=PROFILE, countryCode="US", storeType="vendor")]
            if failure == "conflicting_zone":
                scopes[0]["timezone"] = "UTC"
            return _result(_ok(tool, profiles=scopes))
        assert tool == "get_ads_perf"
        assert args["profileIds"] == [PROFILE] and args["factEntity"] == "campaign"
        assert "campaign.campaignId_" in args["select"] and "profile.profileId_" in args["select"]
        assert "groupBy" not in args and args["filters"] == {"campaign.campaignType_": "sponsoredProducts"}
        hour = args["timeGranularity"] == "hourly"
        page = args["page"]
        rows = [dict(campaignId=509 + i, campaignName=f"Ad {i}", campaignType="sponsoredProducts",
                     profileId=PROFILE, date=args["dateStart"].replace("-", ""),
                     **{metric: (5 if metric in ("Impressions", "Clicks") else 0)
                        for metric in args["metrics"]}, **({"hour": i} if hour else {}))
                for i in ([1, 2] if page == 1 else [3])]
        if failure == "duplicate" and page == 2:
            rows = [{**rows[0], "campaignId": 510, **({"hour": 1} if hour else {})}]
        if failure == "disjoint_reordered_page" and page == 2:
            rows[0]["campaignId"] = 509
        if failure == "future_hour" and hour:
            rows[0]["hour"] = 23
        if failure == "wrong_ad_type":
            rows[0]["campaignType"] = "sponsoredBrands"
        if failure == "wrong_date":
            rows[0]["date"] = "20261125"
        if failure == "wrong_row_profile":
            rows[0]["profileId"] = PROFILE + 1
        if failure == "missing_row_profile":
            rows[0].pop("profileId")
        if failure == "missing_metric":
            rows[0].pop("Spend")
        if failure == "ambiguous_alias":
            rows[0]["campaign.campaignId_"] = rows[0]["campaignId"]
        if failure == "null_orders" and not hour:
            rows[0]["Conversions"] = None
        if failure == "expired_second" and page == 2:
            rows = []
        if failure == "empty":
            rows = []
        more = page == 1 and bool(rows)
        meta = dict(page=page, pageSize=2, currency="USD", effectiveProfileIds=[PROFILE], hasNextPage=more)
        if failure == "wrong_profile":
            meta["effectiveProfileIds"] = [PROFILE + 1]
        if failure == "wrong_currency":
            meta["currency"] = "CAD"
        if failure == "unproven_partial":
            meta["isPartial"] = True
        if failure == "wrong_count":
            rows.pop()
        if failure == "changed_total":
            meta["total"] = 3 if page == 1 else 5
        if failure in {"standard_advice", "source_warns_partial", "source_warns_truncated", "unrecognized_hint"}:
            meta["hint"] = ("Limit conclusions to this query's scope. Missing fields are not zero, and empty "
                            "results do not prove that no matching business data exists. Do not present partial "
                            "or paginated results as complete. Distinguish returned facts from analytical inferences; "
                            "state limitations or query further when evidence is insufficient.")
            if failure == "source_warns_partial" and page == 2:
                meta["hint"] = "Report returned partial_data. " + meta["hint"]
            if failure == "source_warns_truncated":
                meta["hint"] = "供应端仅返回部分活动。 " + meta["hint"]
            if failure == "unrecognized_hint":
                meta["hint"] = "Upstream advisory code 113. " + meta["hint"]
        packet = _ok(tool, data={"rows": rows, "rowCount": len(rows)}, meta=meta)
        if failure == "duplicate_json":
            return _result(None, json.dumps(packet).replace('"page": 1', '"page": 2, "page": 1'))
        if failure == "structured_conflict":
            return _result(packet, json.dumps({**packet, "meta": {**meta, "currency": "CAD"}}))
        if failure == "text_only":
            return _result(None, "```json\n" + json.dumps(packet) + "\n```")
        return _result(packet)

    monkeypatch.setattr(sparkx, "call_sparkx", receive)
    return wire


def _verify(settings=None, store=None, binding=None):
    return anyio.run(lambda: sparkx.verify_sparkx_binding(settings or _settings(), store or _store(),
                                                         binding or BINDING, now=NOW))


def test_backend_requests_one_verified_shop_and_finishes_all_pages(source):
    proof = _verify()
    assert proof["status"] == "VERIFIED" and proof["row_count"] == 3 and proof["sample_date"] == "2026-11-23"
    observed, failed = anyio.run(lambda: sparkx.read_sparkx_observations(_settings(), _store(), BINDING, now=NOW))
    assert failed == {} and set(observed) == {"daily", "hourly"}
    assert observed["daily"]["row_count"] == observed["hourly"]["row_count"] == 3
    assert observed["daily"]["rows"][0]["conversion_basis"] == "provisional"
    assert observed["hourly"]["rows"][0]["conversion_basis"] == "not_requested"
    assert set(observed["hourly"]["rows"][0]["metrics"]) == set(sparkx.HOURLY_METRICS)
    day_and_hour = [(q["timeGranularity"], q["dateStart"], q["page"])
                    for tool, q in source["calls"] if tool == "get_ads_perf"]
    assert day_and_hour == [
        ("daily", "2026-11-23", 1), ("daily", "2026-11-23", 2),
        ("daily", "2026-11-23", 1), ("daily", "2026-11-23", 2),
        ("hourly", "2026-11-24", 1), ("hourly", "2026-11-24", 2),
    ]


def test_vendor_json_text_exactly_proves_scope(source):
    source["failure"] = "text_only"
    assert _verify()["row_count"] == 3


@pytest.mark.parametrize(("mode", "code"), [
    ("wrong_ad_type", "SPARKX_CAMPAIGN_IDENTITY_NOT_PROVEN"),
    ("wrong_profile", "SPARKX_PAGE_SCOPE_MISMATCH"),
    ("wrong_currency", "SPARKX_PAGE_SCOPE_MISMATCH"), ("wrong_count", "SPARKX_PAGE_SCOPE_MISMATCH"),
    ("other_market", "SPARKX_AUTHORIZED_SCOPE_MISMATCH"),
    ("conflicting_zone", "SPARKX_AUTHORIZED_SCOPE_MISMATCH"),
    ("unknown_declared_type", "SPARKX_AUTHORIZED_SCOPE_MISMATCH"),
    ("null_declared_type", "SPARKX_AUTHORIZED_SCOPE_MISMATCH"),
    ("conflicting_roster", "SPARKX_PROFILE_NOT_PROVEN"),
    ("expired_second", "SPARKX_INCOMPLETE_PAGINATION"),
    ("changed_total", "SPARKX_INCOMPLETE_PAGINATION"),
    ("empty", "SPARKX_EMPTY_NOT_ZERO"),
    ("source_warns_partial", "SPARKX_SOURCE_PARTIAL_WARNING"),
    ("source_warns_truncated", "SPARKX_SOURCE_PARTIAL_WARNING"),
    ("unproven_partial", "SPARKX_PAGE_NOT_PROVEN"),
    ("duplicate", "SPARKX_DUPLICATE_CAMPAIGN_GRAIN"),
    ("disjoint_reordered_page", "SPARKX_UNSTABLE_PAGE_ORDER"),
    ("wrong_date", "SPARKX_ROW_SCOPE_MISMATCH"),
    ("wrong_row_profile", "SPARKX_ROW_SCOPE_MISMATCH"),
    ("missing_row_profile", "SPARKX_ROW_FIELDS_NOT_PROVEN"),
    ("ambiguous_alias", "SPARKX_ROW_FIELDS_NOT_PROVEN"),
    ("missing_metric", "SPARKX_METRIC_NOT_PROVEN"), ("null_orders", "SPARKX_METRIC_NOT_PROVEN"),
    ("duplicate_json", "SPARKX_RESULT_NOT_PROVEN"), ("structured_conflict", "SPARKX_RESULT_NOT_PROVEN"),
])
def test_backend_never_uses_unproven_pages_or_fabricates_zero(source, mode, code):
    source["failure"] = mode
    with pytest.raises(sparkx.SparkXBackendError) as exc:
        _verify()
    assert exc.value.code == code


def test_known_market_or_currency_mismatch_blocks_before_any_network(source):
    for store in (_store(status=0), _store(marketplace_id="A2EUQ1WTGCTBG2"), _store(currency_code="CAD"),
                  _store(timezone="UTC"), _store(country="JP")):
        with pytest.raises(sparkx.SparkXBackendError):
            _verify(store=store)
    with pytest.raises(sparkx.SparkXBackendError):
        _verify(settings=Settings(_env_file=None, sparkx_ai_token="test-only", sparkx_profile_ids="1"))
    assert source["calls"] == []


def test_proven_daily_isolated_from_future_hour_without_writes_to_lingxing(source):
    source["failure"] = "future_hour"
    result, errors = anyio.run(lambda: sparkx.read_sparkx_observations(_settings(), _store(), BINDING, now=NOW))
    assert list(result) == ["daily"] and errors == {"hourly": "SPARKX_HOUR_NOT_PROVEN"}
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(**vars(_store()), name="Approved Shop")
        db.add(store)
        slot = NOW.isoformat()
        first = sparkx.persist_sparkx_observation(db, store, CHOICE, result["daily"], observed_at=NOW, slot=slot)
        assert sparkx.persist_sparkx_observation(db, store, CHOICE, result["daily"], observed_at=NOW, slot=slot) == first
        db.commit()
        summary, = sparkx.list_sparkx_snapshots(db, store, CHOICE)
        assert summary["period"] == "daily" and summary["row_count"] == 3 and summary["state"] == "OBSERVED"
        assert "rows" not in summary and "profile_id" not in summary
        assert sparkx.list_sparkx_snapshots(db, store, {**CHOICE, "source": "lingxing"}) == []
        assert sparkx.list_sparkx_snapshots(db, store, {**CHOICE, "selected_at": (NOW + timedelta(seconds=1)).isoformat()}) == []
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 1
        for table in (DailyMetric, DataCoverage, Campaign, NotificationOutbox):
            assert db.scalar(select(func.count()).select_from(table)) == 0
    engine.dispose()


def test_documented_sparse_profile_is_observed_without_guessing_attribution(source):
    source["failure"] = "missing_type"
    assert _verify()["status"] == "VERIFIED"
    result, errors = anyio.run(lambda: sparkx.read_sparkx_observations(_settings(), _store(), BINDING, now=NOW))
    assert errors == {} and result["daily"]["store_type"] == "unknown"
    assert result["daily"]["usage"] == "observation_only_no_notifications"
    assert all(row["conversion_basis"] == "unverified_attribution_window" and "attribution_days" not in row
               for row in result["daily"]["rows"])
    assert all(row["conversion_basis"] == "not_requested" and "attribution_days" not in row
               for row in result["hourly"]["rows"])

def test_generic_caution_does_not_count_as_partial_and_unknown_warning_is_sanitized(source):
    source["failure"] = "standard_advice"
    assert _verify()["provider_warning"] is False
    source["failure"] = "unrecognized_hint"
    proof = _verify()
    assert proof["provider_warning"] is True and proof["coverage"] == "observed_not_certified"
    observed, errors = anyio.run(lambda: sparkx.read_sparkx_observations(_settings(), _store(), BINDING, now=NOW))
    assert errors == {} and all(row["provider_warning"] is True and row["coverage"] == "observed_not_certified"
                                for row in observed.values())
    assert "Upstream advisory code 113" not in json.dumps(observed)

def test_revocation_after_first_page_aborts_remaining_reads(source):
    auth_checks = 0

    def authorize():
        nonlocal auth_checks
        auth_checks += 1
        if auth_checks == 3:
            raise sparkx.SparkXBackendError("SCOPE_CHANGED")

    with pytest.raises(sparkx.SparkXBackendError, match="SCOPE_CHANGED"):
        anyio.run(lambda: sparkx.read_sparkx_observations(
            _settings(), _store(), BINDING, now=NOW, authorize=authorize))
    assert [x[0] for x in source["calls"]] == ["get_user_authorized_context", "get_ads_perf"]


def test_latest_summary_keeps_both_grains_and_cannot_reuse_the_wrong_date(source):
    result, errors = anyio.run(lambda: sparkx.read_sparkx_observations(_settings(), _store(), BINDING, now=NOW))
    assert not errors
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(**vars(_store()), name="Approved Shop")
        db.add(store)
        daily, hourly = result["daily"], result["hourly"]
        sparkx.persist_sparkx_observation(db, store, CHOICE, hourly, observed_at=NOW, slot="old")
        first = sparkx.persist_sparkx_observation(db, store, CHOICE, daily,
                                                  observed_at=NOW + timedelta(minutes=1), slot="test")
        new_daily = {**daily, "report_date": NOW.date().isoformat()}
        second = sparkx.persist_sparkx_observation(db, store, CHOICE, new_daily,
                                                   observed_at=NOW + timedelta(days=1), slot="test")
        db.commit()
        assert first["snapshot_id"] != second["snapshot_id"]
        identified = {item["period"]: item for item in sparkx.list_sparkx_snapshots(db, store, CHOICE)}
        assert set(identified) == {"hourly", "daily"} and identified["daily"]["report_date"] == NOW.date().isoformat()
    engine.dispose()
