"""SparkX direct daily report and 7-day campaign review: data, cards, queue and delivery."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import sparkx_reports as reports
from jx_alert.config import Settings
from jx_alert.notifications import sparkx_report_cards as cards
from jx_alert.notifications.cards import card_fits
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import NotificationLog, SyncJob

DAY, BEFORE = "2026-11-22", "2026-11-21"
W, P = ["2026-11-16", "2026-11-22"], ["2026-11-09", "2026-11-15"]
SEND = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)  # 08:30 Beijing on Nov 24
APP_ID = "cli_sparkx_synthetic"


def m(impressions, clicks, spend, sales, orders, units=None):
    return {"impressions": float(impressions), "clicks": float(clicks), "spend": float(spend),
            "sales": float(sales), "orders": float(orders), "units": float(orders if units is None else units)}


def camp(name, w, p, state="enabled", kind="sponsoredProducts"):
    return {"name": name, "type": kind, "state": state, "w": w, "p": p}


COLLECTED = {
    "generated_at": SEND.isoformat(), "report_date": DAY, "previous_date": BEFORE, "window": W,
    "previous_window": P, "unauthorized_allowed": [], "errors": {},
    "profiles": {"101": {"name": "香港川页", "country": "US", "store_type": "seller"},
                 "102": {"name": "Campx-主账号", "country": "US", "store_type": "seller"},
                 "103": {"name": "FITFIZZ-主账号", "country": "US", "store_type": "seller"},
                 "104": {"name": "陈子俊", "country": "US", "store_type": "seller"},
                 "105": {"name": "昌泉", "country": "US", "store_type": "seller"},
                 "106": {"name": "迈源", "country": "US", "store_type": "seller"},
                 "201": {"name": "川页-CA", "country": "CA", "store_type": "seller"}},
    "countries": {"US": {"profiles": ["101", "102", "103", "104", "105", "106"], "currency": "USD"},
                  "CA": {"profiles": ["201"], "currency": "CAD"}},
    "daily": {"101": {DAY: m(20000, 1500, 3217.70, 10726.72, 239, 256), BEFORE: m(18000, 1300, 2616.0, 9000, 200)},
              "102": {DAY: m(9000, 300, 598.32, 927.93, 7), BEFORE: m(8000, 260, 494.48, 900, 6)},
              "103": {DAY: m(4000, 120, 184.25, 0, 0), BEFORE: m(1500, 40, 61.2, 0, 0)},
              "104": {DAY: m(800, 20, 33.48, 179.99, 1), BEFORE: m(700, 18, 31.0, 160, 1)}},
    "total_sales": {"101": {DAY: 20000.0}, "102": {DAY: 1500.0}, "103": {DAY: 300.0}, "104": {DAY: 400.0}},
    "campaigns": {
        "101": {"1": camp("14MQ-S-SP-queen bed platform-2.45", m(9000, 120, 162, 93, 2), m(8000, 110, 150, 484, 9)),
                "2": camp("S-14MQ-metal bed frame", None, m(5000, 90, 126, 400, 8)),
                "3": camp("Brand-Exact", m(50000, 1500, 1100, 3300, 60), m(48000, 1450, 1000, 3000, 55))},
        "102": {"4": camp("CTH-1012006-ASIN定投-TEST-Down", m(3000, 95, 223, 0, 0), m(2000, 50, 100, 0, 0)),
                "5": camp("CTH-1025-SD-ASIN定投", m(4000, 120, 150, 325, 5), m(3000, 90, 100, 500, 8),
                          kind="sponsoredDisplay")},
        "103": {"6": camp("Asin定投 [x](https://evil.example) <at id=ou_x>", m(6000, 313, 303, 0, 0), None)},
        "104": {"7": camp("陈子俊-Auto", m(2000, 60, 290, 1620, 9), m(2100, 62, 280, 1500, 8))},
    },
    "enabled_campaigns": {"105": 44, "106": 0, "201": 0},
}


def test_name_suggestions_match_same_site_and_leave_unknown_for_owner():
    profiles = {"1": {"name": "香港川页", "country": "US"}, "2": {"name": "XPE-主账号", "country": "US"},
                "3": {"name": "FITFIZZ-主账号", "country": "US"}, "4": {"name": "昌泉", "country": "US"},
                "5": {"name": "OCTORA-主账号", "country": "US"}, "6": {"name": "聚星同创（US seller）", "country": "US"},
                "7": {"name": "黄陈柳", "country": "US"}, "8": {"name": "川页-CA", "country": "CA"},
                "9": {"name": "薛兵娟", "country": "US"}, "10": {"name": "HASSKY-主账号", "country": "US"}}
    stores = [{"sid": "9060", "name": "川页-US", "country": "US"}, {"sid": "9061", "name": "川页-CA", "country": "CA"},
              {"sid": "5207", "name": "耐森-XPE-主账号-US-US", "country": "US"},
              {"sid": "5177", "name": "羡森-FitFizz-主账号-US", "country": "US"},
              {"sid": "6890", "name": "AMZ-义乌昌泉-主账号-US-US", "country": "US"},
              {"sid": "6914", "name": "octora-主账号-库博-DE", "country": "DE"},
              {"sid": "8128", "name": "聚星同创-US", "country": "US"}, {"sid": "8438", "name": "黄陈柳-US", "country": "US"},
              {"sid": "8311", "name": "陈子俊-US", "country": "US"},
              {"sid": "5173", "name": "G-shark-HASSKY-主账号-US", "country": "US"}]
    assert reports.suggest_routes(profiles, stores) == {
        "1": "9060", "2": "5207", "3": "5177", "4": "6890", "6": "8128", "7": "8438", "8": "9061", "10": "5173"}
    assert reports.short_name("XPE-主账号") == "XPE" and reports.short_name("川页-CA") == "川页"
    assert reports.short_name("聚星同创（US seller）") == "聚星同创（US seller）"


def test_daily_report_keeps_missing_as_missing_and_explains_in_agent_tone():
    report = reports.build_daily(COLLECTED, {"101": "皮泽霖", "102": "高爽"})
    us, ca = report["sites"]
    assert (us["code"], us["store_count"], us["with_data"]) == ("US", 6, 4)
    assert us["current"]["spend"] == pytest.approx(3217.70 + 598.32 + 184.25 + 33.48)
    assert [row["name"] for row in us["stores"]] == ["香港川页", "Campx", "FITFIZZ", "陈子俊"]
    assert us["stores"][0]["spend_change"] == pytest.approx(3217.70 / 2616 - 1)
    assert us["quiet"] == ["昌泉", "迈源"]
    assert ca["current"] is None and ca["empty"] and ca["with_data"] == 0
    notes = "\n".join(report["notes"])
    assert "加拿大共 1 个店铺，SparkX 当日没有返回任何广告数据（不是 0" in notes
    assert "店铺间分化较大" in notes and "香港川页 一家店占花费的 80%" in notes
    assert "Campx 花费 $598，只带回 $928，ACOS 64.5%，为全站最高。" in notes
    assert "FITFIZZ 花费 $184，销售额为 0，120 次点击没有出单。" in notes
    assert "是全站效率最好的" not in notes  # the cheapest ACOS is the already-described top store
    value = cards.daily_card(report)
    assert card_fits(value) and sum(e["tag"] == "table" for e in value["body"]["elements"]) == 2
    text = cards.plain_text(value)
    assert "美国 | USD | 6 | 4 | 4,033.75 | 11,834.64 | 264 | 247 | 34.1% | 2.93" in text
    assert "加拿大 | CAD | 1 | 0 | — | — | — | — | — | — | — | — | — | — | —" in text
    assert "香港川页 | 皮泽霖 | 3,217.70 | 10,726.72 | 256 | 239 | 30.0% | +23%" in text
    assert "FITFIZZ | 未映射运营 | 184.25 | 0.00 | 0 | 0 | — | +201%" in text
    assert "美国当日无花费：昌泉、迈源" in text


def test_anomaly_categories_store_overview_and_suggestions():
    report = reports.build_anomalies(COLLECTED, {"101": "皮泽霖", "102": "高爽"})
    found = {item["cid"]: item["category"] for item in report["findings"]}
    assert found == {"1": "acos_worse", "2": "stalled", "4": "zero_orders", "5": "surge", "6": "zero_orders"}
    assert [row["pid"] for row in report["zero_stores"]] == ["105"]
    assert {row["pid"] for row in report["idle_stores"]} == {"106", "201"}
    assert report["store_total"] == 7 and report["active_total"] == 4
    overview = {row["pid"]: row for row in report["overview"]}
    assert overview["101"]["count"] == 2 and overview["101"]["problem"] == "1 个停摆、1 个 ACOS 恶化"
    assert {row["pid"]: row["name"] for row in report["idle_stores"]} == {"106": "迈源", "201": "川页（加拿大）"}
    assert overview["102"]["problem"].endswith("1 个零出单、1 个加预算后销售下滑")
    assert [item["cid"] for item in report["grouped"]["zero_orders"]] == ["6", "4"]  # by spend
    text = "\n".join(report["suggestions"])
    assert text.startswith("昌泉 先查账户状态") and "名称含 TEST/测试，可考虑直接归档" in text
    value = cards.anomaly_card(report, mentions=["ou_owner"])
    assert card_fits(value) and sum(e["tag"] == "table" for e in value["body"]["elements"]) == 5
    plain = cards.plain_text(value)
    assert "7 个店铺中，4 个有投放数据，3 个检出 5 个异常 campaign。" in plain
    assert "一、最严重：全店零曝光" in plain and "44 个 campaign 全部是 enabled 状态" in plain
    assert "SparkX 没有返回任何曝光、花费或销售记录" in plain and "0 曝光" not in plain
    assert "二、异常店铺总览" in plain and "三、按问题类型看具体 campaign" in plain and "四、建议动作" in plain
    assert "香港川页 | 皮泽霖 | S-14MQ-metal bed frame | $126 | 无投放记录" in plain
    assert "香港川页 | 皮泽霖 | 14MQ-S-SP-queen bed platform-2.45 | 31% → 174% | $162 | $93.00" in plain
    assert "Campx | 高爽 | CTH-1025-SD-ASIN定投 | +50%（$150） | -35% | $150" in plain
    assert "另有 2 个店铺零数据" in plain
    markdown = "\n".join(e["content"] for e in value["body"]["elements"] if e["tag"] == "markdown")
    assert "<at id=ou_owner></at>" in markdown  # the only mention is the explicit one
    assert "](" not in markdown and "<at id=ou_x>" not in markdown  # provider text cannot inject markup


def test_operator_slice_only_contains_their_stores_and_none_when_clean():
    report = reports.build_anomalies(COLLECTED, {"101": "皮泽霖", "102": "高爽"})
    value = cards.anomaly_card(report, only={"102"})
    text = cards.plain_text(value)
    assert "你负责的店铺中，1 个检出 2 个异常 campaign。" in text
    assert "香港川页" not in text and "昌泉" not in text and "建议动作" not in text
    assert cards.anomaly_card(report, only={"104"}) is None


def test_non_usd_rows_are_not_judged_with_usd_thresholds():
    data = copy.deepcopy(COLLECTED)
    data["countries"]["US"]["currency"] = "MXN"
    assert reports.build_anomalies(data, {})["findings"] == []


class FakeSession:
    def __init__(self, *, fail_total_sales=False, partial=False):
        self.calls, self.fail_total_sales, self.partial = [], fail_total_sales, partial

    async def call_tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_user_authorized_context":
            return self._result({"isError": False, "toolName": name, "userId": "u", "tenantId": "t", "profiles": [
                {"profileId": 101, "profileName": "香港川页", "countryCode": "US", "storeType": "seller"},
                {"profileId": "105", "profileName": "昌泉", "countryCode": "US"},
                {"profileId": 201, "profileName": "川页-CA", "countryCode": "CA"}]})
        ids = [str(pid) for pid in arguments["profileIds"]]
        page = arguments["page"]
        meta = {"page": page, "pageSize": arguments["pageSize"], "effectiveProfileIds": arguments["profileIds"],
                "hint": "Limit conclusions to this query's scope." + (" Partial result." if self.partial else "")}
        if name == "get_entity_metadata":
            rows = [{"profileId": "105", "campaignState": "enabled", "currency": "USD"}] * (44 if "105" in ids else 0)
            return self._page(name, rows, {**meta, "hasNextPage": False})
        currency = "USD" if "101" in ids else "CAD"
        meta["currency"] = currency
        if arguments["factEntity"] == "asin":
            if self.fail_total_sales:
                return self._result({"isError": True, "toolName": name, "errorType": "invalid_params"})
            rows = [{"date": "20261122", "profileId_": "101", "TotalSalesAmount": "20000.5"}] if "101" in ids else []
            return self._page(name, rows, {**meta, "hasNextPage": False})
        if "date" in arguments["select"]:
            rows = [{"date": "20261122", "profileId": 101, "profileName": "香港川页", "Impressions": 100, "Clicks": 10,
                     "Spend": 50.5, "Sales": 200, "Conversions": 4, "Units": 5},
                    {"date": "2026-11-21", "profile.profileId_": "101", "Impressions": 90, "Clicks": 9,
                     "Spend": 40, "Sales": 100, "Conversions": 2, "Units": 2}] if "101" in ids else []
            return self._page(name, rows, {**meta, "hasNextPage": False})
        if "101" not in ids:
            return self._page(name, [], {**meta, "hasNextPage": False})
        if page == 1:  # two pages for the current window, proving pagination is followed
            rows = [{"profileId": "101", "campaignId": "11", "campaignName": "A", "campaignType": "sponsoredProducts",
                     "campaignState": "enabled", "Impressions": 10, "Clicks": 1, "Spend": 5, "Sales": 0,
                     "Conversions": 0, "Units": 0}]
            return self._page(name, rows, {**meta, "hasNextPage": arguments["dateStart"] == W[0]})
        rows = [{"profileId": "101", "campaignId": "12", "campaignName": "B", "campaignType": "sponsoredBrands",
                 "campaignState": "paused", "Impressions": 20, "Clicks": 2, "Spend": 6, "Sales": 10,
                 "Conversions": 1, "Units": 1}]
        return self._page(name, rows, {**meta, "hasNextPage": False})

    def _page(self, name, rows, meta):
        return self._result({"isError": False, "toolName": name, "data": {"rows": rows, "rowCount": len(rows)},
                             "meta": meta, "requestId": "r"})

    @staticmethod
    def _result(value):
        return CallToolResult(content=[], structuredContent=value)


def _collect(monkeypatch, session, allowed=("101", "105", "201", "999")):
    @asynccontextmanager
    async def fake(token, timeout):
        yield session

    monkeypatch.setattr(reports, "_session", fake)
    monkeypatch.setattr(reports, "_credentials", lambda settings: ("token-synthetic-0001", list(allowed)))
    monkeypatch.setattr(reports.anyio, "sleep", lambda *_: _noop())
    return reports.collect(SimpleNamespace(), now=SEND)


async def _noop():
    return None


def test_collector_reads_only_documented_tools_with_pagination_and_safe_failures(monkeypatch):
    session = FakeSession()
    data = _collect(monkeypatch, session)
    assert {name for name, _ in session.calls} == {"get_user_authorized_context", "get_ads_perf",
                                                    "get_entity_metadata"}
    assert data["unauthorized_allowed"] == ["999"] and set(data["profiles"]) == {"101", "105", "201"}
    assert data["countries"]["US"] == {"profiles": ["101", "105"], "currency": "USD"}
    assert data["daily"]["101"][DAY]["spend"] == 50.5 and data["daily"]["101"][BEFORE]["orders"] == 2
    assert "105" not in data["daily"]  # no row is not a zero row
    assert data["total_sales"]["101"][DAY] == 20000.5
    assert set(data["campaigns"]["101"]) == {"11", "12"}
    assert data["campaigns"]["101"]["12"]["state"] == "paused"
    assert data["enabled_campaigns"] == {"105": 44, "201": 0}
    perf = [args for name, args in session.calls if name == "get_ads_perf"]
    assert all(args["pageSize"] == 500 and args["language"] == "en" and "userContext" in args for args in perf)
    assert all(999 not in args["profileIds"] for args in perf)
    assert data["report_date"] == DAY and data["window"] == W and data["previous_window"] == P
    tolerant = _collect(monkeypatch, FakeSession(fail_total_sales=True))
    assert tolerant["total_sales"] is None and tolerant["errors"]["total_sales"] == "SPARKX_INVALID_PARAMS"
    with pytest.raises(reports.SparkXReportError) as caught:
        _collect(monkeypatch, FakeSession(partial=True))
    assert caught.value.code == "SPARKX_SOURCE_PARTIAL"


def test_collector_requires_token_and_allowed_profiles(monkeypatch):
    monkeypatch.setattr(reports, "_credentials", lambda settings: (None, []))
    with pytest.raises(reports.SparkXReportError) as caught:
        reports.collect(SimpleNamespace(), now=SEND)
    assert caught.value.code == "SPARKX_NOT_CONFIGURED"
    monkeypatch.setattr(reports, "_credentials", lambda settings: ("token-synthetic-0001", []))
    with pytest.raises(reports.SparkXReportError) as caught:
        reports.collect(SimpleNamespace(), now=SEND)
    assert caught.value.code == "SPARKX_NO_PROFILES"


@pytest.fixture
def lane(tmp_path, monkeypatch):
    routing = {"default_chat_id": "reports",
               "groups": {"reports": {"chat_id": "oc_reports", "display_name": "日报群"}},
               "stores": {"9060": {"chat_id": "reports", "open_ids": ["ou_lead"],
                                   "owner_display_names": {"ou_lead": "皮泽霖"}},
                          "5171": {"chat_id": "reports", "open_ids": ["ou_gao"],
                                   "owner_display_names": {"ou_gao": "高爽"}}},
               "ad_alert_subscribers": {"recipients": [{"open_id": "ou_lead", "name": "皮泽霖", "generation": "a" * 32}]},
               "company_executive_digest": {"open_id": "ou_boss", "name": "总负责人", "app_id": APP_ID,
                                            "generation": "b" * 32, "verified_at": "2026-11-01T00:00:00+00:00"}}
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(routing), encoding="utf-8")
    (tmp_path / "scope.json").write_text(json.dumps({"store_ids": [9060, 5171]}), encoding="utf-8")
    settings = Settings(_env_file=None, app_dry_run=False, scheduler_enabled=True, feishu_enabled=True,
                        feishu_transport="app", feishu_app_id=APP_ID, feishu_app_secret=SecretStr("synthetic-secret"),
                        feishu_daily_report_chat_id="reports", feishu_routing_file=str(path),
                        collection_scope_file=str(tmp_path / "scope.json"),
                        store_controls_file=str(tmp_path / "controls.json"),
                        feishu_quiet_delivery_enabled=True, hourly_ad_monitor_enabled=True,
                        sparkx_report_settings_file=str(tmp_path / "sparkx-report.json"),
                        sparkx_credentials_file=str(tmp_path / "sparkx.json"),
                        sparkx_ai_token=SecretStr("token-synthetic-0001"), sparkx_profile_ids="101,102,103,104,105,106,201")
    config = reports.load_config(settings)
    reports.save_config(settings, {"revision": config["revision"], "enabled": True, "daily_source": "sparkx",
        "destinations": {**reports.DESTINATIONS, "anomaly_operators": True}, "routes": {"101": "9060", "102": "5171"}})
    engine = create_engine("sqlite:///" + str(tmp_path / "lane.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    yield settings, sessions, routing, path
    engine.dispose()


def _queue(settings, sessions, now=SEND, collected=COLLECTED):
    with sessions.begin() as db:
        return reports.queue_reports(db, settings, now=now, collected=copy.deepcopy(collected))


def test_queue_freezes_one_card_per_recipient_and_maps_operators(lane):
    settings, sessions, _, _ = lane
    result = _queue(settings, sessions)
    assert result["status"] == "QUEUED" and len(result["outbox_ids"]) == 4
    with sessions() as db:
        rows = {row.payload["delivery"]["role"]: row for row in db.scalars(select(NotificationOutbox))}
        assert set(rows) == {"sparkx_daily_lead", "sparkx_anomaly_group", "sparkx_anomaly_lead",
                             "sparkx_anomaly_operator"}
        assert rows["sparkx_daily_lead"].chat_id == "ou_boss"
        assert rows["sparkx_anomaly_lead"].chat_id == "ou_lead"
        assert rows["sparkx_anomaly_operator"].chat_id == "ou_gao"  # the lead is not messaged twice
        assert rows["sparkx_anomaly_operator"].payload["delivery"]["profiles"] == ["102"]
        group = json.dumps(rows["sparkx_anomaly_group"].payload["card"], ensure_ascii=False)
        assert "<at id=ou_lead></at>" in group and "<at id=ou_gao></at>" in group
        assert all(row.payload["sparkx_report_day"] == "2026-11-24" for row in rows.values())
        assert rows["sparkx_daily_lead"].payload["store_ids"] == ["5171", "9060"]
        assert "皮泽霖" in cards.plain_text(rows["sparkx_daily_lead"].payload["card"])
        job = db.scalar(select(SyncJob).where(SyncJob.job_type == reports.JOB))
        assert job.status == "SUCCEEDED" and job.metadata_json["findings"] == 5
    assert _queue(settings, sessions)["status"] == "ALREADY_COMPLETE"
    with sessions() as db:
        assert len(list(db.scalars(select(NotificationOutbox)))) == 4


def test_paused_store_is_removed_from_every_card(lane):
    settings, sessions, _, _ = lane
    Path(settings.store_controls_file).write_text(json.dumps({"version": 1, "stores": {"5171": {
        "collection_enabled": True, "notifications_enabled": False, "updated_at": SEND.isoformat()}}}))
    _queue(settings, sessions)
    with sessions() as db:
        rows = list(db.scalars(select(NotificationOutbox)))
    assert rows and all("Campx" not in json.dumps(row.payload["card"], ensure_ascii=False) for row in rows)
    assert "sparkx_anomaly_operator" not in {row.payload["delivery"]["role"] for row in rows}


@pytest.mark.parametrize("change,expected", [
    ("disabled", "DISABLED"), ("early", "WAITING"), ("late", "WAITING"), ("dry_run", "DELIVERY_DISABLED"),
])
def test_queue_respects_switches_and_generation_window(lane, change, expected):
    settings, sessions, _, _ = lane
    now = SEND
    if change == "disabled":
        config = reports.load_config(settings)
        reports.save_config(settings, {**{k: config[k] for k in ("enabled", "daily_source", "destinations", "routes")},
                                       "revision": config["revision"], "enabled": False})
    elif change == "early":
        now = SEND - timedelta(minutes=1)
    elif change == "late":
        now = SEND + timedelta(minutes=90)
    else:
        settings.app_dry_run = True
    assert _queue(settings, sessions, now=now)["status"] == expected


def test_failed_collection_is_recorded_and_retried_later(lane, monkeypatch):
    settings, sessions, _, _ = lane

    def fail(*args, **kwargs):
        raise reports.SparkXReportError("SPARKX_AUTH_REJECTED")

    monkeypatch.setattr(reports, "collect", fail)
    with sessions.begin() as db:
        assert reports.queue_reports(db, settings, now=SEND)["status"] == "FAILED"
    with sessions.begin() as db:
        assert reports.queue_reports(db, settings, now=SEND + timedelta(minutes=5))["status"] == "RETRY_LATER"
    with sessions.begin() as db:
        assert reports.queue_reports(db, settings, now=SEND + timedelta(minutes=11))["status"] == "FAILED"


def test_company_digest_yields_to_sparkx_daily_source(lane):
    settings, _, _, _ = lane
    assert reports.supersedes_company_digest(settings)
    settings.sparkx_ai_token = SecretStr("")
    assert not reports.supersedes_company_digest(settings)


@pytest.fixture
def worker_lane(lane, monkeypatch):
    from jx_alert.notifications import worker
    from jx_alert.notifications.feishu_app import FeishuAppClient

    settings, sessions, routing, path = lane
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "load_runtime_settings", lambda: settings)
    clock = SimpleNamespace(now=SEND + timedelta(minutes=1))

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", TestClock)
    posted = []

    def send(value, *, before_submit, **args):
        before_submit({"app_identity_sha256": "x"})
        posted.append(args["receive_id"])
        return {"message_id": "om_" + args["receive_id"]}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: SimpleNamespace(send_card=send, send_text=send))
    _queue(settings, sessions)
    return settings, sessions, routing, path, clock, posted, worker


def test_worker_delivers_reports_under_quiet_and_hourly_policies(worker_lane):
    settings, sessions, _, _, _, posted, worker = worker_lane
    assert worker.process_outbox_batch()["sent"] == 4
    assert sorted(posted) == ["oc_reports", "ou_boss", "ou_gao", "ou_lead"]
    with sessions() as db:
        assert {row.status for row in db.scalars(select(NotificationOutbox))} == {"DELIVERED"}
        assert db.scalar(select(NotificationLog.message_id).where(NotificationLog.status == "DELIVERED")).startswith("om_")


@pytest.mark.parametrize("change,status", [
    ("lane_disabled", "SKIPPED"), ("lead_changed", "SKIPPED"), ("operator_unmapped", "SKIPPED"),
    ("after_noon", "EXPIRED"),
])
def test_worker_rechecks_owner_switches_and_recipients(worker_lane, change, status):
    settings, sessions, routing, path, clock, posted, worker = worker_lane
    if change == "lane_disabled":
        config = reports.load_config(settings)
        reports.save_config(settings, {**{k: config[k] for k in ("daily_source", "destinations", "routes")},
                                       "revision": config["revision"], "enabled": False})
    elif change == "lead_changed":
        routing["company_executive_digest"]["open_id"] = "ou_someone_else"
        routing["ad_alert_subscribers"]["recipients"][0]["generation"] = "c" * 32
        path.write_text(json.dumps(routing), encoding="utf-8")
    elif change == "operator_unmapped":
        routing["stores"]["5171"]["open_ids"] = ["ou_new_operator"]
        path.write_text(json.dumps(routing), encoding="utf-8")
    else:
        clock.now = SEND + timedelta(hours=3, minutes=31)
    worker.process_outbox_batch()
    with sessions() as db:
        states = {row.payload["delivery"]["role"]: row.status for row in db.scalars(select(NotificationOutbox))}
    if change == "lane_disabled" or change == "after_noon":
        assert set(states.values()) == {status} and posted == []
    elif change == "lead_changed":
        assert states["sparkx_daily_lead"] == states["sparkx_anomaly_lead"] == "SKIPPED"
        assert states["sparkx_anomaly_group"] == "DELIVERED"
    else:
        assert states["sparkx_anomaly_operator"] == "SKIPPED" and "ou_gao" not in posted


def test_owner_settings_are_private_validated_and_compare_and_swap(lane):
    settings, _, _, _ = lane
    path = Path(settings.sparkx_report_settings_file)
    config = reports.load_config(settings)
    assert config["routes"] == {"101": "9060", "102": "5171"}
    base = {k: config[k] for k in ("enabled", "daily_source", "destinations", "routes")}
    with pytest.raises(reports.SparkXReportConfigError) as stale:
        reports.save_config(settings, {**base, "revision": "0" * 64})
    assert stale.value.status_code == 409
    for bad in ({"daily_source": "excel"}, {"routes": {"abc": "1"}}, {"destinations": {"daily_group": True}},
                {"enabled": "yes"}):
        with pytest.raises(reports.SparkXReportConfigError) as invalid:
            reports.save_config(settings, {**base, **bad, "revision": config["revision"]})
        assert invalid.value.status_code == 400
    cleared = reports.save_config(settings, {**base, "routes": {"101": None}, "revision": config["revision"]})
    assert cleared["routes"] == {}
    import os
    import stat
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
