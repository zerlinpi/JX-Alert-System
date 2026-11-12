from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert.lingxing.sp_persistence import (
    SPPersistenceError, latest_sp_partition, persist_sp_validation,
)
from jx_alert.lingxing.sp_validation import (
    DAILY_PATH, HOUR_ENDPOINTS, MASTER_PATH, collect_sp_validation,
)
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, RawSnapshot
from jx_alert.storage.sp import (
    CollectionLog, ReconciliationLog, SPAdvertiseDetail, SPDailyReport,
    SPHourlyReport, SPTargetDetail,
)


SID, CID, PROFILE, DAY = 5174, 141469378857743, 1251791933842067, "2026-11-11"


def report_row(cost="93.20", **changes):
    return {"campaign_id": CID, "profile_id": PROFILE, "report_date": DAY,
            "cost": cost, "clicks": 57, "impressions": 15176,
            "orders": 5, "sales": "789.92", **changes}


class Client:
    def __init__(self, *, daily_cost="93.20", hourly_cost="87.72", failed=(),
                 daily_rows=None, empty_hours=False):
        self.daily_cost, self.hourly_cost = daily_cost, hourly_cost
        self.failed, self.daily_rows, self.empty_hours = failed, daily_rows, empty_hours

    def _request(self, method, path, *, json_body, advertising):
        if path in self.failed:
            raise RuntimeError("a signed URL or credential must not enter logs")
        if path == MASTER_PATH:
            rows = [{"campaign_id": CID, "profile_id": PROFILE, "name": "Target campaign",
                     "state": "enabled", "start_date": "2026-11-09", "end_date": None}]
        elif path == DAILY_PATH:
            rows = ([report_row(self.daily_cost)] if self.daily_rows is None
                    else deepcopy(self.daily_rows))
        else:
            rows = [] if self.empty_hours else [report_row(
                self.hourly_cost, clicks=54, impressions=14364, hour=9,
                group_id=52178430164077, ad_id=424301765477967,
                targeting_id=238054384505019, asin="B0D4DJMNRZ", msku="GTH-722005-2B",
                match_type="BROAD", targeting="adjustable ball mount",
            )]
        return {"code": 0, "data": rows, "total": len(rows), "next_token": None,
                "request_id": "request-" + path.rsplit("/", 1)[-1]}


@pytest.fixture
def db(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "sp.sqlite"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def collect(db, client=None):
    return collect_sp_validation(client or Client(), db, sid=SID, campaign_id=CID,
                                 report_dates=[DAY], request_interval=0)


def latest(db, scope="daily"):
    return latest_sp_partition(db, sid=SID, campaign_id=CID, report_date=DAY, scope=scope)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_persist_real_contract_versions_all_four_scopes_and_raw_detail(db):
    result = collect(db)
    persisted = persist_sp_validation(db, result)
    assert not persisted["reused"]
    assert persisted["row_counts"] == dict(daily=1, campaign=1, ad_group=1, ad=1, target=1)
    assert count(db, CollectionLog) == 1
    assert count(db, ReconciliationLog) == 5
    assert count(db, SPHourlyReport) == 2
    assert count(db, DailyMetric) == 0
    assert latest(db)["metrics"]["cost"] == Decimal("93.20")
    for scope in ["campaign", "ad_group", "ad", "target"]:
        partition = latest(db, scope)
        assert partition["metrics"]["cost"] == Decimal("87.72")
        assert partition["comparison_status"] == "DIFFERENT"
        assert not partition["stale_after_failure"]
    detail = latest(db, "ad")["rows"][0]
    assert detail.ad_id == "424301765477967"
    assert detail.group_id == "52178430164077"
    assert detail.asin == "B0D4DJMNRZ" and detail.seller_sku == "GTH-722005-2B"
    assert detail.source_snapshot_id is not None and detail.request_id
    assert detail.raw_json["cost"] == "87.72"
    target = latest(db, "target")["rows"][0]
    assert target.targeting == "adjustable ball mount" and target.match_type == "BROAD"
    recon = db.scalar(select(ReconciliationLog).where(ReconciliationLog.scope == "campaign"))
    assert recon.delta_hour_minus_daily["cost"] == "-5.48"
    assert recon.daily_metrics["cost"] == "93.20"
    assert recon.hourly_metrics["cost"] == "87.72"


def test_replay_same_run_does_not_accumulate_cost_or_rows(db):
    result = collect(db)
    persist_sp_validation(db, result)
    db.commit()
    repeated = persist_sp_validation(db, deepcopy(result))
    assert repeated["reused"]
    assert count(db, CollectionLog) == 1 and count(db, SPDailyReport) == 1
    assert latest(db)["metrics"]["cost"] == Decimal("93.20")
    assert count(db, SPAdvertiseDetail) == 1 and count(db, SPTargetDetail) == 1


def test_new_run_appends_version_but_latest_selects_one_partition(db):
    first = collect(db)
    persist_sp_validation(db, first)
    second = collect(db, Client(daily_cost="94.30", hourly_cost="88.12"))
    persist_sp_validation(db, second)
    assert count(db, SPDailyReport) == 2
    assert latest(db)["run_id"] == second["run_id"]
    assert latest(db)["metrics"]["cost"] == Decimal("94.30")
    assert latest(db, "target")["metrics"]["cost"] == Decimal("88.12")
    assert db.scalar(select(SPDailyReport.cost).where(
        SPDailyReport.run_id == first["run_id"])) == Decimal("93.20")


def test_failed_new_partition_keeps_old_version_and_marks_it_stale(db):
    first = collect(db)
    persist_sp_validation(db, first)
    second = collect(db, Client(failed=[DAILY_PATH, HOUR_ENDPOINTS["ad"][0]]))
    persisted = persist_sp_validation(db, second)
    assert persisted["status"] == "PARTIAL"
    assert count(db, SPDailyReport) == 1
    current = latest(db)
    assert current["run_id"] == first["run_id"] and current["stale_after_failure"]
    assert current["latest_attempt_status"] == "REQUEST_FAILED"
    assert latest(db, "ad")["stale_after_failure"]
    assert latest(db, "target")["run_id"] == second["run_id"]
    failed_log = db.scalar(select(ReconciliationLog).where(
        ReconciliationLog.run_id == second["run_id"], ReconciliationLog.scope == "daily"))
    assert failed_log.daily_metrics is None and failed_log.error_code == "REQUEST_ERROR"
    assert "signed URL" not in json.dumps(db.get(CollectionLog, second["run_id"]).summary)


@pytest.mark.parametrize("rows,expected", [([], "NO_RECORDS"),
                                          ([report_row(campaign_id=CID + 1)], "UNMATCHED")])
def test_successfully_empty_or_unmatched_supersedes_previous_rows(db, rows, expected):
    persist_sp_validation(db, collect(db))
    result = collect(db, Client(daily_rows=rows, empty_hours=True))
    persist_sp_validation(db, result)
    current = latest(db)
    assert current["run_id"] == result["run_id"]
    assert current["status"] == expected and current["rows"] == []
    assert current["metrics"] is None and not current["stale_after_failure"]
    assert latest(db, "campaign")["status"] == "NO_RECORDS"


def test_explicit_zero_remains_a_real_value(db):
    result = collect(db, Client(daily_cost="0", hourly_cost="0"))
    persist_sp_validation(db, result)
    current = latest(db)
    assert current["status"] == "ZERO_SPEND"
    assert current["metrics"]["cost"] == Decimal(0)
    assert len(current["rows"]) == 1


def test_database_reconnect_retains_immutable_history(db):
    result = collect(db)
    persist_sp_validation(db, result)
    db.commit()
    engine = db.get_bind()
    db.close()
    engine.dispose()
    with Session(engine) as reopened:
        assert latest(reopened)["metrics"]["cost"] == Decimal("93.20")
        assert reopened.get(CollectionLog, result["run_id"]).status == "COMPLETE"


def test_changed_summary_with_same_run_id_is_rejected(db):
    result = collect(db)
    persist_sp_validation(db, result)
    changed = deepcopy(result)
    changed["days"][0]["daily"]["metrics"]["cost"] = "999"
    with pytest.raises(SPPersistenceError, match="IMMUTABLE_RUN_CONFLICT"):
        persist_sp_validation(db, changed)
    assert latest(db)["metrics"]["cost"] == Decimal("93.20")


def test_caller_rollback_reverts_facts_even_when_raw_evidence_was_committed(db):
    result = collect(db)
    db.commit()
    persist_sp_validation(db, result)
    db.rollback()
    assert count(db, CollectionLog) == 0
    assert count(db, SPDailyReport) == 0
    assert count(db, RawSnapshot) > 0


def test_summary_cannot_replace_raw_response_values(db):
    result = collect(db)
    result["days"][0]["daily"]["metrics"]["cost"] = "999"
    with pytest.raises(SPPersistenceError, match="SUMMARY_RAW_MISMATCH"):
        persist_sp_validation(db, result)
    assert count(db, CollectionLog) == 0 and count(db, SPDailyReport) == 0
    assert count(db, RawSnapshot) > 0


def test_missing_raw_evidence_is_rejected_without_creating_facts(db):
    result = collect(db)
    db.query(RawSnapshot).delete()
    with pytest.raises(SPPersistenceError, match="MISSING_RAW_EVIDENCE"):
        persist_sp_validation(db, result)
    assert count(db, CollectionLog) == 0


def test_duplicate_raw_page_is_not_double_counted(db):
    result = collect(db)
    page = db.scalar(select(RawSnapshot).where(
        RawSnapshot.payload["endpoint"].as_string() == DAILY_PATH))
    db.add(RawSnapshot(source=page.source, store_id=page.store_id,
                       captured_at=page.captured_at, payload=deepcopy(page.payload)))
    with pytest.raises(SPPersistenceError, match="RAW_PARTITION_INCOMPLETE"):
        persist_sp_validation(db, result)
    assert count(db, SPDailyReport) == 0


def test_latest_versions_are_selected_by_capture_time_not_insert_order(db):
    first = collect(db, Client(daily_cost="10"))
    for snapshot in db.scalars(select(RawSnapshot)).all():
        snapshot.captured_at = datetime(2026, 11, 12, 12)
    persist_sp_validation(db, first)
    second = collect(db, Client(daily_cost="20"))
    for snapshot in db.scalars(select(RawSnapshot).where(
            RawSnapshot.payload["run_id"].as_string() == second["run_id"])).all():
        snapshot.captured_at = datetime(2026, 11, 12, 12) - timedelta(hours=1)
    persist_sp_validation(db, second)
    assert latest(db)["run_id"] == first["run_id"]
    assert latest(db)["metrics"]["cost"] == Decimal("10")


def test_failure_without_previous_complete_version_is_unavailable(db):
    result = collect(db, Client(failed=[DAILY_PATH]))
    persist_sp_validation(db, result)
    current = latest(db)
    assert current["status"] == "UNAVAILABLE" and current["metrics"] is None
    assert current["latest_attempt_status"] == "REQUEST_FAILED"
