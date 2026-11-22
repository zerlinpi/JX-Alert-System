"""Acceptance on isolated warehouse copies only; no live/provider/delivery clients."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from jx_alert.lingxing.api_business import FINANCE, PRODUCT, SOURCE
from jx_alert.reporting.owner_today import prepare_owner_today
from jx_alert.storage.db import Base
from jx_alert.storage.models import DataCoverage, DailyMetric, Store


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/audit_owner_today.py"
spec = importlib.util.spec_from_file_location("owner_today_audit", SCRIPT)
audit_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_cli)
CLOCK = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)
DAY_LA = date(2026, 11, 21)
PACIFIC = "America/Los_Angeles"
FIELDS = {"adSpend": 20, "netAmount": 99.5, "volume": 8, "returnGoodsCount": 2}


def store(db, sid, zone=PACIFIC, *, status=1):
    db.add(Store(sid=sid, name="店铺" + sid, timezone=zone, status=status))


def partition(db, sid, dimension, *, day=DAY_LA, at=CLOCK, count=1, status="COMPLETE",
              facts=None, currency="USD", source=SOURCE, entity=None):
    db.add(DataCoverage(store_id=sid, metric_date=day, dimension=dimension, row_count=count,
                        status=status, completed_at=at.astimezone(timezone.utc).replace(tzinfo=None)))
    for key, val in (facts or {}).items():
        db.add(DailyMetric(store_id=sid, metric_date=day, dimension=dimension,
            entity_id=entity or (sid if dimension == FINANCE else "A"), metric_name=key, value=val,
            currency=currency, source=source))


@pytest.fixture
def warehouse():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def text(value):
    return json.dumps(value, ensure_ascii=False)


def test_mixed_store_dates_timezones_and_real_currency_totals_stay_separate(warehouse):
    db = warehouse
    for sid in ("1", "2", "3"):
        store(db, sid)
    for sid, zone in (("4", "Asia/Tokyo"), ("5", "America/New_York")):
        store(db, sid, zone)
    for sid, facts, currency in (("1", FIELDS, "USD"), ("2", {**FIELDS, "adSpend": 0, "netAmount": 0,
        "volume": 2, "returnGoodsCount": 0}, "USD"), ("3", FIELDS, "EUR"),
        ("4", {**FIELDS, "adSpend": 777}, "USD"), ("5", {**FIELDS, "adSpend": 888}, "USD")):
        day = date(2026, 11, 22) if sid == "4" else DAY_LA
        partition(db, sid, PRODUCT, facts=facts, day=day, currency=currency)
        partition(db, sid, FINANCE, facts={"totalSalesRefunds": -7 if sid != "2" else 0},
                  day=day, currency=currency)
    db.commit()
    result = prepare_owner_today(db, ["1", "2", "3", "4", "5"], now=CLOCK)
    assert result["status"] == "ALL_METRICS_PROVISIONAL" and result["delivery_enabled"] is False
    assert result["provider_updated_at"] is None and not result["blocked_stores"]
    groups = {(g["report_date"], g["timezone"]): g for g in result["groups"]}
    assert len(groups) == 3
    group = groups[(DAY_LA.isoformat(), PACIFIC)]
    summary, detail = group["cards"]
    table = next(e for e in summary["body"]["elements"] if e["tag"] == "table")
    summary_rows = [(r["c0"], r["c1"], r["c2"]) for r in table["rows"]]
    assert ("广告花费", "USD 20.00", "2/3 店") in summary_rows
    assert ("净销售额", "USD 99.50", "2/3 店") in summary_rows
    assert ("退款金额", "EUR 7.00", "1/3 店") in summary_rows
    assert ("合计件数退货率", "22.22%", "3/3 店") in summary_rows  # (2+0+2)/(8+2+8)
    assert "USD 0.00" in text(detail) and "未结束，仅供参考" in text(detail)
    assert "来源更新时间未验证" in text(summary)
    assert all("America/Los_Angeles 日期组 3 店 / 指定范围 5 店" in text(c) for c in (summary, detail))
    assert "777.00" not in text(group) and "888.00" not in text(group)
    assert "777.00" in text(groups[("2026-11-22", "Asia/Tokyo")])
    assert "888.00" in text(groups[(DAY_LA.isoformat(), "America/New_York")])
    assert group["stores"][0]["sources"][PRODUCT]["warehouse_completed_at"] == CLOCK.isoformat()


@pytest.mark.parametrize("kind,expected", [
    ("absent", "MISSING"), ("empty", "EMPTY"), ("failed", "NOT_COMPLETE"),
    ("stale", "STALE_CAPTURE"), ("future", "FUTURE"), ("other_day", "WRONG_CAPTURE_DAY"),
    ("other_source", "CURRENCY_OR_FACTS_UNVERIFIED"), ("incomplete", "MISSING_OR_INVALID_FIELD"),
    ("mixed_currency", "CURRENCY_OR_FACTS_UNVERIFIED"), ("negative_rows", "INVALID_ROW_COUNT"),
])
def test_unusable_product_evidence_never_becomes_business_values(warehouse, kind, expected):
    db = warehouse
    store(db, "1")
    target = CLOCK if kind != "other_day" else datetime(2026, 11, 21, 8, 1, tzinfo=timezone.utc)
    # Just before local midnight may be only a few minutes old, but covers another local day.
    at = {"future": target + timedelta(seconds=1), "stale": target - timedelta(minutes=90, seconds=1),
          "other_day": target - timedelta(minutes=2)}.get(kind, target)
    if kind != "absent":
        count = {"incomplete": 2, "mixed_currency": 2, "empty": 0, "negative_rows": -1}.get(kind, 1)
        partition(db, "1", PRODUCT, at=at, count=count, facts=FIELDS,
                  status="FAILED" if kind == "failed" else "COMPLETE",
                  source="another_provider" if kind == "other_source" else SOURCE)
    if kind == "mixed_currency":
        for key, val in FIELDS.items():
            db.add(DailyMetric(store_id="1", metric_date=DAY_LA, dimension=PRODUCT, entity_id="B",
                               metric_name=key, value=val, currency="CAD", source=SOURCE))
    # Neither a completed prior day nor today's hourly SP partition is an acceptable fallback.
    partition(db, "1", PRODUCT, day=DAY_LA-timedelta(days=1), facts={"adSpend": 111111})
    partition(db, "1", "campaign_sp", facts={"adSpend": 222222})
    db.commit()
    result = prepare_owner_today(db, ["1"], now=target)
    group, = result["groups"]
    record, = group["stores"]
    assert result["status"] == "NO_PREVIEW_VALUES" and not group["cards"]
    assert record["metrics"]["ad_spend"] == expected and record["metrics"]["refund_amount"] == "MISSING"
    assert "111111" not in text(result) and "222222" not in text(result)


def test_cutoff_dst_scope_and_field_level_exclusions_are_explicit(warehouse):
    db = warehouse
    cutoff = datetime(2026, 11, 1, 9, 20, tzinfo=timezone.utc)  # pacific fallback, 01:20 PST
    store(db, "1")
    store(db, "2", "Invalid/Zone")
    store(db, "3", status=2)
    store(db, "5")
    day = date(2026, 11, 1)
    partition(db, "1", PRODUCT, day=day, at=cutoff-timedelta(minutes=90),
              facts={"adSpend": 0, "volume": 0, "returnGoodsCount": 2})
    partition(db, "1", FINANCE, day=day, at=cutoff, currency="CAD", facts={"totalSalesRefunds": -123456})
    partition(db, "5", PRODUCT, day=day, at=cutoff, facts={"adSpend": 999999})  # outside explicit scope
    db.commit()
    result = prepare_owner_today(db, [1, 2, 3, 4], now=cutoff)
    assert result["scope_count"] == 4 and result["status"] == "PARTIAL_PREVIEW"
    assert [(row["store_id"], row["reason"]) for row in result["blocked_stores"]] == [
        ("2", "TIMEZONE_UNVERIFIED"), ("3", "NO_ACTIVE_INVENTORY"), ("4", "NO_ACTIVE_INVENTORY")]
    check, = result["groups"][0]["stores"]
    assert check["metrics"] == {"ad_spend": "PROVISIONAL_VALUE", "sales": "MISSING_OR_INVALID_FIELD",
        "return_rate": "ZERO_DENOMINATOR", "refund_amount": "CURRENCY_CONFLICT"}
    assert "999999" not in text(result) and "123456" not in text(result)
    assert "0.00%" not in text(result["groups"][0]["cards"])


def test_exact_settlement_only_and_failed_product_do_not_mask_each_other(warehouse):
    db = warehouse
    store(db, "1")
    partition(db, "1", PRODUCT, status="FAILED", facts=FIELDS)
    partition(db, "1", FINANCE, facts={"totalSalesRefunds": -12})
    db.commit()
    result = prepare_owner_today(db, ["1"], now=CLOCK)
    assert result["status"] == "PARTIAL_PREVIEW"
    group, = result["groups"]
    assert group["stores"][0]["metrics"]["refund_amount"] == "PROVISIONAL_VALUE"
    assert "USD 12.00" in text(group["cards"]) and "已发放结算口径" in text(group["cards"])
    assert "99.50" not in text(group["cards"])


def test_script_reads_only_standalone_copy_and_never_creates_missing_db(tmp_path):
    path = tmp_path / "QA ? 副本.sqlite"
    scope = tmp_path / "scope.json"
    scope.write_text('{"store_ids":[1]}')
    with pytest.raises(FileNotFoundError):
        audit_cli.audit(path, scope, now=CLOCK)
    assert not path.exists()
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(path))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store(db, "1")
        partition(db, "1", PRODUCT, facts=FIELDS)
        db.commit()
    engine.dispose()
    before = hashlib.sha256(path.read_bytes()).digest()
    seen = []
    from sqlalchemy.engine import Engine
    def assert_readonly(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.split()[0])
        assert statement.startswith("SELECT")
    event.listen(Engine, "before_cursor_execute", assert_readonly)
    try:
        result = audit_cli.audit(path, scope, now=CLOCK)
    finally:
        event.remove(Engine, "before_cursor_execute", assert_readonly)
    assert seen and result["status"] == "PARTIAL_PREVIEW"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert sorted(tmp_path.iterdir()) == sorted([scope, path])
    sidecar = Path(str(path) + "-wal")
    sidecar.touch()
    with pytest.raises(ValueError, match="standalone"):
        audit_cli.audit(path, scope)


@pytest.mark.parametrize("scope", [None, [], [True], ["0"], ["01"], ["1", "bad"], "all"])
def test_missing_or_invalid_scope_never_expands(warehouse, scope):
    with pytest.raises(ValueError, match="scope"):
        prepare_owner_today(warehouse, scope, now=CLOCK)


def test_default_cli_refuses_to_start_without_an_explicit_copy_and_scope():
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT,
        capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode != 0 and b"--sqlite-copy" in result.stderr and b"--scope-file" in result.stderr


def test_mismatched_asin_sets_and_wrong_finance_identity_cannot_supply_same_window(warehouse):
    db = warehouse
    store(db, "1")
    partition(db, "1", PRODUCT, facts={"returnGoodsCount": 4}, entity="A")
    db.add(DailyMetric(store_id="1", metric_date=DAY_LA, dimension=PRODUCT, entity_id="B",
                       metric_name="volume", value=2, currency="USD", source=SOURCE))
    partition(db, "1", FINANCE, facts={"totalSalesRefunds": -28}, entity="other_store")
    db.commit()
    result = prepare_owner_today(db, ["1"], now=CLOCK)
    assert result["status"] == "NO_PREVIEW_VALUES" and result["groups"][0]["cards"] == []
    metrics = result["groups"][0]["stores"][0]["metrics"]
    assert metrics["return_rate"] != "PROVISIONAL_VALUE" and metrics["refund_amount"] != "PROVISIONAL_VALUE"


def test_fixture_ingestion_to_today_preview_keeps_provider_clock_unknown(warehouse, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from jx_alert.lingxing.api_business import collect_api_business_for_day
    db = warehouse
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK.astimezone(tz)

    monkeypatch.setattr("jx_alert.storage.coverage.datetime", FixedClock)
    local_day = DAY_LA.isoformat()
    store(db, "1")
    db.commit()

    class SyntheticProvider:
        def product_performance_daily(self, sid, day, **page):
            return {"data": {"total": 1, "list": [{"sids": [sid], "asins": [{"sid": sid, "asin": "A"}],
                "currency_code": "USD", "spend": 17, "return_goods_count": 2, "net_amount": 61, "volume": 3}]}}

        def profit_report_daily(self, sid, day, **page):
            return {"data": {"total": 1, "records": [{"sid": sid, "postedDateLocale": day,
                "currencyCode": "USD", "totalSalesRefunds": -8}]}}

    receipt = collect_api_business_for_day(SyntheticProvider(), db, SimpleNamespace(
        store_controls_file=str(tmp_path / "switches.json")), {"data": [{"sid": 1, "status": 1}]}, local_day)
    assert receipt["status"] == "COMPLETE"
    result = prepare_owner_today(db, ["1"], now=CLOCK)
    assert result["status"] == "ALL_METRICS_PROVISIONAL"
    assert result["provider_updated_at"] is None and result["delivery_enabled"] is False
    assert "USD 61.00" in text(result["groups"][0]["cards"])


def test_unrenderable_and_overflowing_fields_are_never_counted_as_preview_values(warehouse):
    db = warehouse
    store(db, "1")
    partition(db, "1", PRODUCT, facts={"netAmount": 1e308, "adSpend": 1e29}, count=2)
    for name, value in (("netAmount", 1e308), ("adSpend", 0)):
        db.add(DailyMetric(store_id="1", metric_date=DAY_LA, dimension=PRODUCT, entity_id="B",
                           metric_name=name, value=value, currency="USD", source=SOURCE))
    db.commit()
    result = prepare_owner_today(db, ["1"], now=CLOCK)
    assert result["status"] == "NO_PREVIEW_VALUES"
    assert result["groups"][0]["cards"] == []


def test_cli_does_not_load_ambient_runtime_settings_or_database_url(tmp_path):
    path = tmp_path / "sealed.sqlite"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(path))
    Base.metadata.create_all(engine)
    engine.dispose()
    scope = tmp_path / "scope.json"
    scope.write_text('{"store_ids":["1"]}')
    poison = tmp_path / "poison.json"
    poison.write_text("malformed runtime settings")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "RUNTIME_SETTINGS_FILE": str(poison),
           "MCP_AUTH_FILE": str(poison), "DATABASE_URL": "not-a-warehouse-url"}
    help_only = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=tmp_path,
                               capture_output=True, env=env)
    assert help_only.returncode == 0
    run = subprocess.run([sys.executable, str(SCRIPT), "--sqlite-copy", str(path),
                          "--scope-file", str(scope)], cwd=tmp_path, capture_output=True, env=env)
    assert run.returncode == 2 and json.loads(run.stdout)["status"] == "NO_PREVIEW_VALUES"
    assert not run.stderr
