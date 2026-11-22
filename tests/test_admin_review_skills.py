import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jx_alert import admin_review_skills as admin
from jx_alert.ai.ad_review_skills import DEFAULT_SKILL, load_ad_review_skills, skill_fingerprint


def test_install_native_policy_changes_worker_and_preserves_owner_overrides(tmp_path):
    path = tmp_path / "policy.json"
    settings = SimpleNamespace(ad_review_skills_file=str(path))
    document = {
        "version": 1,
        "defaults": {"min_clicks": 75},
        "ad_types": {"sp": {"min_spend_usd": 150}},
        "campaigns": {"10:sp:102": {"objective": "reach", "learning_days": 14}},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    before = load_ad_review_skills(settings)
    state = admin.get_review_skills(settings)
    assert state["active_preset_id"] == "balanced"
    assert state["preserved_overrides"] == {"ad_types": 1, "campaigns": 1}
    changed = admin.install_review_skill(settings, {"revision": state["revision"], "preset_id": "efficiency"})
    expected = {**document, "defaults": {**document["defaults"], "objective": "efficiency"}}
    assert json.loads(path.read_bytes()) == expected
    assert changed["revision"] != state["revision"]
    assert changed["active_preset_id"] == "efficiency"
    assert str(path) not in str(changed)
    after = load_ad_review_skills(settings)
    assert after.for_campaign("10", "sp", "101")["objective"] == "efficiency"
    assert after.for_campaign("10", "sp", "101")["min_spend_usd"] == 150
    assert after.for_campaign("10", "sp", "101")["min_clicks"] == 75
    assert skill_fingerprint(after.for_campaign("10", "sp", "101")) != skill_fingerprint(
        before.for_campaign("10", "sp", "101")
    )
    assert after.for_campaign("10", "sp", "102") == before.for_campaign("10", "sp", "102")
    with pytest.raises(admin.ReviewSkillError) as stale:
        admin.install_review_skill(settings, {"revision": state["revision"], "preset_id": "discovery"})
    assert stale.value.status_code == 409
    assert json.loads(path.read_bytes()) == expected


def test_no_policy_file_default_noop_then_create_and_reload(tmp_path):
    path = tmp_path / "policy.json"
    settings = SimpleNamespace(ad_review_skills_file=str(path))
    initial = admin.get_review_skills(settings)
    assert initial["status"] == "default"
    assert initial["active_preset_id"] == DEFAULT_SKILL["objective"]
    assert initial["can_install"] is True
    assert admin.install_review_skill(settings, {"revision": initial["revision"], "preset_id": "balanced"}) == initial
    assert not path.exists()
    active = admin.install_review_skill(settings, {"revision": initial["revision"], "preset_id": "discovery"})
    assert active["status"] == "configured"
    assert admin.get_review_skills(settings) == active
    assert not (path.stat().st_mode & 0o077)


@pytest.mark.parametrize("payload", [
    {"preset_id": "https://example.com/skill.sh"},
    {"preset_id": "balanced", "path": "/tmp/script", "version": 1},
    {"preset_id": ["balanced"]},
    {"preset_id": "unknown"},
])
def test_rejects_untrusted_preset_identifiers_and_arbitrary_source_options(tmp_path, payload):
    path = tmp_path / "policy.json"
    settings = SimpleNamespace(ad_review_skills_file=str(path))
    payload = {"revision": admin.get_review_skills(settings)["revision"], **payload}
    with pytest.raises(admin.ReviewSkillError) as bad:
        admin.install_review_skill(settings, payload)
    assert bad.value.status_code == 400
    assert not path.exists()


def test_invalid_file_and_symlink_not_replaced(tmp_path):
    path = tmp_path / "policy.json"
    settings = SimpleNamespace(ad_review_skills_file=str(path))
    initial = admin.get_review_skills(settings)
    target = tmp_path / "owner-config.json"
    target.write_text('{"version":1}', encoding="utf-8")
    for mode in ("invalid", "link"):
        if mode == "invalid":
            path.write_text('{"version":1,"version":1}', encoding="utf-8")
        else:
            path.unlink()
            path.symlink_to(target)
        state = admin.get_review_skills(settings)
        assert state["status"] == "invalid"
        assert state["revision"] is None
        assert state["can_install"] is False
        assert load_ad_review_skills(settings).status == "invalid"
        with pytest.raises(admin.ReviewSkillError) as refusal:
            admin.install_review_skill(settings, {"revision": initial["revision"], "preset_id": "reach"})
        assert refusal.value.status_code == 409
        assert target.read_text(encoding="utf-8") == '{"version":1}'
        if mode == "invalid":
            assert path.read_text(encoding="utf-8") == '{"version":1,"version":1}'
        else:
            assert path.is_symlink()


def test_concurrent_writer_fails_closed(tmp_path):
    path = Path(tmp_path / "policy.json")
    settings = SimpleNamespace(ad_review_skills_file=str(path))
    revision = admin.get_review_skills(settings)["revision"]
    with admin._write_lock(path), pytest.raises(admin.ReviewSkillError) as conflict:
        admin.install_review_skill(settings, {"revision": revision, "preset_id": "reach"})
    assert conflict.value.status_code == 409
    assert not path.exists()


def test_unconfigured_storage_is_disabled():
    settings = SimpleNamespace(ad_review_skills_file="")
    assert admin.get_review_skills(settings)["status"] == "disabled"
    with pytest.raises(admin.ReviewSkillError) as disabled:
        admin.install_review_skill(settings, {"revision": "0" * 64, "preset_id": "reach"})
    assert disabled.value.status_code == 409
