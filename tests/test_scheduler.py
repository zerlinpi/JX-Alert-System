"""Scheduler jobs: lanes run independently and just-queued rows are sent at once."""
from datetime import timedelta

from jx_alert import scheduler
from jx_alert.notifications import worker
from jx_alert.sparkx import collect as collect_module
from jx_alert.storage import db as storage_db
from test_report_lanes import HOUR, SEND, collector_for, enable
from test_sparkx_analysis import DAILY, HOURLY, REVIEW


def wire(monkeypatch, settings, session_factory, data_by_lane):
    monkeypatch.setattr(storage_db, "SessionLocal", session_factory)
    monkeypatch.setattr(storage_db, "engine", session_factory.engine)
    monkeypatch.setattr(scheduler, "load_runtime_settings", lambda: settings)
    lanes_seen, sent = [], []

    def fake_collect(settings_, lane, *, now, directory, on_roster):
        lanes_seen.append(lane)
        return collector_for(data_by_lane[lane])(settings_, lane, now=now, directory=directory, on_roster=on_roster)

    monkeypatch.setattr(collect_module, "collect", fake_collect)
    monkeypatch.setattr(worker, "process_outbox_batch", lambda **kwargs: sent.append(kwargs["outbox_ids"]) or {"sent": 1})
    return lanes_seen, sent


def test_morning_job_runs_both_lanes_and_sends_what_it_queued(monkeypatch, settings, session_factory):
    enable(settings, daily={"enabled": True, "ai": False}, review={"enabled": True, "ai": False})
    lanes_seen, sent = wire(monkeypatch, settings, session_factory, {"daily": DAILY, "review": REVIEW})
    scheduler.morning_job(now=SEND - timedelta(minutes=5))
    assert lanes_seen == [] and sent == []
    scheduler.morning_job(now=SEND)
    assert lanes_seen == ["daily", "review"] and len(sent) == 2
    scheduler.morning_job(now=SEND + timedelta(minutes=5))
    assert lanes_seen == ["daily", "review"]  # already complete for today


def test_one_failing_lane_does_not_stop_the_other(monkeypatch, settings, session_factory):
    enable(settings, daily={"enabled": True, "ai": False}, review={"enabled": True, "ai": False})
    lanes_seen, sent = wire(monkeypatch, settings, session_factory, {"review": REVIEW})
    scheduler.morning_job(now=SEND)  # daily raises KeyError inside the fake collector
    assert lanes_seen == ["daily", "review"] and len(sent) == 1


def test_hourly_job(monkeypatch, settings, session_factory):
    enable(settings, hourly={"enabled": True})
    lanes_seen, sent = wire(monkeypatch, settings, session_factory, {"hourly": HOURLY})
    scheduler.hourly_job(now=HOUR)
    assert lanes_seen == ["hourly"] and len(sent) == 1
