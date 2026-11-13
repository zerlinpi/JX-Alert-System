import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.notifications.chat_report import enqueue_chat_report
from jx_alert.notifications.outbox import NotificationOutbox


def test_chat_report_preserves_markdown_and_uses_only_configured_group(tmp_path):
    routing = tmp_path / "route.json"
    routing.write_text(json.dumps({"default_chat_id": "handover", "groups": {
        "handover": {"chat_id": "oc_handover", "display_name": "独立站交接群", "admin_open_ids": ["ou_admin"]},
    }}))
    settings = SimpleNamespace(feishu_routing_file=str(routing), feishu_daily_report_chat_id="handover",
                               feishu_transport="app")
    engine = create_engine("sqlite://")
    NotificationOutbox.__table__.create(engine)
    body = "## 结果\n\n- **已核对**\n\n| 店铺 | 订单 |\n| --- | --- |\n| 甲 | 5 |"
    with Session(engine) as db:
        result = enqueue_chat_report(db, settings, title="经营日报", markdown=body)
        assert enqueue_chat_report(db, settings, title="经营日报", markdown=body) == result
        assert result["group"] == "独立站交接群" and result["status"] == "QUEUED"
        row = db.scalar(select(NotificationOutbox))
        assert row.payload["delivery"]["receive_id"] == "oc_handover"
        assert row.payload["delivery"]["open_ids"] == []
        assert row.payload["card"]["body"]["elements"][0]["content"] == body
        with pytest.raises(ValueError, match="容量"):
            enqueue_chat_report(db, settings, title="经营日报", markdown="太长" * 10000)
        escaped = enqueue_chat_report(db, settings, title="经营日报", markdown="原文 <at id=all></at>")
        assert "<at" not in db.get(NotificationOutbox, escaped["outbox_id"]).payload["card"]["body"]["elements"][0]["content"]
