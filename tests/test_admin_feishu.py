"""Console edits of the routing file: leads, report group and the verified roster."""
import json
from pathlib import Path

import pytest

from conftest import APP_ID, LEAD
from jx_alert import admin_feishu
from jx_alert.reports import recipients

ROSTER = {LEAD: "老板", "ou_adlead": "广告主管", "ou_new": "新同事", "ou_op1": "皮泽霖"}


@pytest.fixture
def feishu(monkeypatch):
    monkeypatch.setattr(admin_feishu, "members", lambda settings, chat: dict(ROSTER))
    monkeypatch.setattr(admin_feishu, "joined_chats",
                        lambda settings, selected=None: {"oc_reports": "日报群", "oc_new": "新群"})


def routing_file(settings):
    return json.loads(Path(settings.feishu_routing_file).read_text(encoding="utf-8"))


def test_company_lead_is_bound_to_the_configured_app_and_keeps_a_backup(settings, feishu):
    view = admin_feishu.leads(settings)
    assert view["company"] == {"id": LEAD, "name": "老板"} and view["group"] == "日报群"
    saved = admin_feishu.save_lead(settings, "company", ["ou_new"], view["revision"])
    assert saved["company"] == {"id": "ou_new", "name": "新同事"}
    binding = routing_file(settings)["company_executive_digest"]
    assert binding["app_id"] == APP_ID and len(binding["generation"]) == 32
    assert recipients.company_lead(settings, routing_file(settings))["open_id"] == "ou_new"
    backups = list(Path(settings.feishu_routing_file).parent.glob("feishu-routing.json.before-console-*"))
    assert backups and all(json.loads(path.read_text(encoding="utf-8")) for path in backups)


def test_ad_lead_keeps_generation_for_the_same_person_and_can_be_cleared(settings, feishu):
    view = admin_feishu.leads(settings)
    before = routing_file(settings)["ad_alert_subscribers"]["recipients"][0]["generation"]
    admin_feishu.save_lead(settings, "ad", ["ou_adlead"], view["revision"])
    assert routing_file(settings)["ad_alert_subscribers"]["recipients"][0]["generation"] == before
    cleared = admin_feishu.save_lead(settings, "ad", [], admin_feishu.leads(settings)["revision"])
    assert cleared["ad"] is None and recipients.ad_lead(routing_file(settings)) is None


def test_non_members_and_stale_revisions_are_rejected(settings, feishu):
    view = admin_feishu.leads(settings)
    with pytest.raises(admin_feishu.MappingError) as exc:
        admin_feishu.save_lead(settings, "company", ["ou_stranger"], view["revision"])
    assert exc.value.status_code == 400
    with pytest.raises(admin_feishu.MappingError) as exc:
        admin_feishu.save_lead(settings, "company", ["ou_new"], "0" * 64)
    assert exc.value.status_code == 409


def test_switching_the_report_group_retires_the_old_chat(settings, feishu):
    current = admin_feishu.current_group(settings)
    saved = admin_feishu.save_group(settings, "oc_new", current["revision"], True)
    assert saved["chat_id"] == "oc_new" and saved["name"] == "新群"
    group = routing_file(settings)["groups"]["reports"]
    assert "oc_reports" in group["retired_chat_ids"] and group["notifications_changed_at"]
    assert group["verified_member_open_ids"] == sorted(ROSTER)
    with pytest.raises(admin_feishu.MappingError):
        admin_feishu.save_group(settings, "oc_new", saved["revision"], False)


def test_verified_people_refreshes_the_cached_roster(settings, feishu):
    people = admin_feishu.verified_people(settings, ["ou_new", "ou_op1"])
    assert people == [{"open_id": "ou_new", "name": "新同事"}, {"open_id": "ou_op1", "name": "皮泽霖"}]
    assert "ou_new" in routing_file(settings)["groups"]["reports"]["verified_member_open_ids"]
