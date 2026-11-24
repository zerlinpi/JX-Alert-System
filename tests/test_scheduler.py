"""Scheduler jobs: lanes run independently and just-queued rows are sent at once."""
from datetime import timedelta

from jx_alert import scheduler
from jx_alert.notifications import worker
from jx_alert.reports import tasks
from jx_alert.storage import db as storage_db
from lane_fakes import HOUR, SEND, runner_for
from test_report_lanes import enable


def wire(monkeypatch, settings, session_factory, *, failing=()):
    monkeypatch.setattr(storage_db, "SessionLocal", session_factory)
    monkeypatch.setattr(storage_db, "engine", session_factory.engine)
    monkeypatch.setattr(scheduler, "load_runtime_settings", lambda: settings)
    lanes_seen, sent = [], []
    fake = runner_for(calls=lanes_seen)

    def run_lane(settings_, lane, config, **kwargs):
        if lane in failing:
            lanes_seen.append(lane)
            raise KeyError("broken")
        return fake(settings_, lane, config, **kwargs)

    monkeypatch.setattr(tasks, "run_lane", run_lane)
    monkeypatch.setattr(worker, "process_outbox_batch", lambda **kwargs: sent.append(kwargs["outbox_ids"]) or {"sent": 1})
    return lanes_seen, sent


def test_morning_job_runs_both_lanes_and_sends_what_it_queued(monkeypatch, settings, session_factory):
    lanes_seen, sent = wire(monkeypatch, settings, session_factory)
    scheduler.morning_job(now=SEND - timedelta(minutes=5))
    assert lanes_seen == [] and sent == []
    scheduler.morning_job(now=SEND)
    assert lanes_seen == ["daily", "review"] and len(sent) == 2
    scheduler.morning_job(now=SEND + timedelta(minutes=5))
    assert lanes_seen == ["daily", "review"]  # already complete for today


def test_one_failing_lane_does_not_stop_the_other(monkeypatch, settings, session_factory):
    lanes_seen, sent = wire(monkeypatch, settings, session_factory, failing={"daily"})
    scheduler.morning_job(now=SEND)  # daily raises KeyError inside DeepSeek's stand-in
    assert lanes_seen == ["daily", "review"] and len(sent) == 1


def test_hourly_job(monkeypatch, settings, session_factory):
    enable(settings, hourly={"enabled": True})
    lanes_seen, sent = wire(monkeypatch, settings, session_factory)
    scheduler.hourly_job(now=HOUR)
    assert lanes_seen == ["hourly"] and len(sent) == 1
