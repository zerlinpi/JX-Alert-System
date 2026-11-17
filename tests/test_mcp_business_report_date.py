"""MCP keeps an unfinished marketplace day distinct from a complete daily report."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jx_alert import mcp_server
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store


@pytest.mark.parametrize("zone,hour,expected", [
    ("America/Los_Angeles", 7, False),
    ("America/Los_Angeles", 8, True),
    (None, 8, None),
    ("unconfirmed/source-zone", 8, None),
])
def test_daily_summary_marks_partial_day_and_unconfirmed_zone(monkeypatch, zone, hour, expected):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(Store(sid="101", name="CAMPX", status=1, timezone=zone))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 17, hour, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(mcp_server, "datetime", Clock)
    monkeypatch.setattr(mcp_server, "init_db", lambda: None)
    monkeypatch.setattr(mcp_server, "SessionLocal", sessions)
    result = mcp_server.get_daily_summary("101", "2026-11-16")
    assert result["report_date_is_complete"] is expected
    assert result["business_daily"]["observation_only"] is (expected is not True)
    if expected is not True:
        assert "不能作为完整日报" in result["report_date_warning"]
    else:
        assert "report_date_warning" not in result
    engine.dispose()
