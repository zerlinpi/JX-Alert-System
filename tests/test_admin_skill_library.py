import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from jx_alert.admin_skill_library import LibraryError, inspect_skill, list_skills, update_skills
from jx_alert.agent_skill_document import parse_upload


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(agent_skill_library_dir=str(tmp_path / "isolated-library"))


TEXT = """---
name: ad-advice
description: >
  用于观察只读数据
  并准备复核。
metadata:
  author: local
  jx-review-objective: efficiency
---
# 示例

本文中的工具名、链接和自然语言不授予操作权限。
"""


def upload(text=TEXT, revision=0, filename="SKILL.md"):
    raw = text if isinstance(text, bytes) else text.encode()
    return {"action": "upload", "filename": filename, "revision": revision,
            "content_base64": base64.b64encode(raw).decode()}


def mutate(settings, action, rev, **more):
    return update_skills(settings, {"action": action, "name": "ad-advice", "revision": rev, **more})


def test_lifecycle_inert_versions_audit_and_reopen(settings):
    root = Path(settings.agent_skill_library_dir)
    assert list_skills(settings)["skills"] == [] and not root.exists()
    text1 = TEXT.replace("\n", "\r\n")
    first = update_skills(settings, upload(text1))
    assert len(first["skills"]) == 1 and "source" not in first["skills"][0]
    assert first["skills"][0]["status"] == "disabled" and first["revision"] == 1
    data = inspect_skill(settings, "ad-advice")
    assert data["document"]["source"] == text1 and data["document"]["digest"] == sha256(text1.encode()).hexdigest()
    assert data["local_preview"] is None and not data["applied_to_model"]
    assert update_skills(settings, upload(text1, 1)) == first  # replay after refresh = no audit churn
    with pytest.raises(LibraryError, match="刷新"):
        mutate(settings, "enable", 0)
    mutate(settings, "enable", 1)
    enabled = inspect_skill(settings, "ad-advice")
    assert enabled["enabled"] and enabled["local_preview"] == {
        "schema": "jx.review-objective.v1", "objective": "efficiency"}
    assert enabled["applied_to_model"] is False
    new = update_skills(settings, upload(TEXT.replace("efficiency", "reach"), 2))
    assert new["skills"][0]["version"] == 2 and not new["skills"][0]["enabled"]
    mutate(settings, "enable", 3)
    assert inspect_skill(settings, "ad-advice", 1)["local_preview"] is None
    mut = mutate(settings, "disable", 4)
    assert not mut["skills"][0]["enabled"]
    with pytest.raises(LibraryError, match="历史版本相同"):
        update_skills(settings, upload(text1, 5))
    with pytest.raises(LibraryError) as gone:
        mutate(settings, "rollback", 5, version=9)
    assert gone.value.status_code == 404
    reopened = SimpleNamespace(agent_skill_library_dir=str(root))
    rolled = mutate(reopened, "rollback", 5, version=1)
    assert rolled["skills"][0]["version"] == 1 and not rolled["skills"][0]["enabled"]
    details = inspect_skill(reopened, "ad-advice")
    assert len(details["versions"]) == 2
    assert [row["action"] for row in details["audit"]] == ["rollback", "disable", "enable", "upload", "enable", "upload"]
    assert all(row["actor"] == "console_owner" for row in details["audit"])
    assert list(root.iterdir()) == [root / "library.sqlite3"]
    assert not root.stat().st_mode & 0o077 and not (root / "library.sqlite3").stat().st_mode & 0o077


@pytest.mark.parametrize("payload", [
    upload(filename="skill.zip"), upload(filename="scripts/skill.py"), upload(filename="../SKILL.md"),
    upload(b"\x00not text"), upload(b"\xff"), upload("x" * (128 * 1024 + 1)), upload("name: missing"),
    upload(TEXT.replace("name: ad-advice", "name: ../outside")),
    upload(TEXT.replace("name: ad-advice", "name: bad--name")),
    upload(TEXT.replace("description: >", "name: duplicate\ndescription: >")),
    upload(TEXT.replace("metadata:", "allowed-tools: Read\nmetadata:")),
    upload(TEXT.replace("metadata:", "workflow: https://example.invalid/run\nmetadata:")),
    upload(TEXT.replace("author: local", "author: &nickname local")),
    upload(TEXT.replace("author: local", "author: {nested: {still: more}}")),
    upload(TEXT.replace("author: local", "author: 42")),
    upload(TEXT.replace("author: local", "author: \"\\u0000\"")),
    upload(TEXT.replace("efficiency", "not-a-supported-objective")),
    {**upload(), "url": "https://example.invalid/SKILL.md"},
    {**upload(), "content_base64": "not base64??"}, {**upload(), "revision": False},
    {"action": [], "revision": 0},
])
def test_rejected_uploads_do_not_create_any_file(settings, payload):
    with pytest.raises(LibraryError) as err:
        update_skills(settings, payload)
    assert err.value.status_code in {400, 413}
    assert not Path(settings.agent_skill_library_dir).exists()


def test_inert_markdown_and_no_model_or_remote_side_effects(settings, monkeypatch):
    import socket
    from jx_alert.ai.ad_review_skills import load_ad_review_skills

    def denied(*args, **kwargs):
        raise AssertionError("Network is not part of the document library")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    settings.ad_review_skills_file = str(Path(settings.agent_skill_library_dir).with_name("native.json"))
    before = load_ad_review_skills(settings)
    doc = TEXT.replace("  jx-review-objective: efficiency\n", "") + """
Example link: https://example.invalid/never-resolved
Example markup: <b>render literally</b>
"""
    update_skills(settings, upload(doc))
    mutate(settings, "enable", 1)
    current = inspect_skill(settings, "ad-advice")
    assert current["document"]["source"] == doc
    assert current["local_preview"] is None and not current["applied_to_model"]
    assert load_ad_review_skills(settings) == before
    assert not Path(settings.ad_review_skills_file).exists()


def test_serialized_stale_writers_one_winner(settings):
    update_skills(settings, upload())

    def attempt(objective):
        try:
            return update_skills(settings, upload(TEXT.replace("efficiency", objective), 1))["revision"]
        except LibraryError as e:
            return e.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, ("balanced", "discovery"))) == [2, 409]
    assert len(inspect_skill(settings, "ad-advice")["versions"]) == 2


def test_private_storage_disabled_unsafe_or_wrong_schema_never_overwritten(settings, tmp_path):
    no = SimpleNamespace(agent_skill_library_dir="")
    assert not list_skills(no)["can_upload"]
    with pytest.raises(LibraryError):
        update_skills(no, upload())
    target = tmp_path / "other"
    target.mkdir(mode=0o700)
    root = Path(settings.agent_skill_library_dir)
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(LibraryError):
        update_skills(settings, upload())
    assert not list(target.iterdir())
    root.unlink()
    root.mkdir(mode=0o700)
    other = target / "original.json"
    other.write_text("{}")
    path = root / "library.sqlite3"
    path.symlink_to(other)
    with pytest.raises(LibraryError):
        update_skills(settings, upload())
    assert other.read_text() == "{}" and path.is_symlink()
    path.unlink()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE keep_me (id INTEGER)")
    db.close()
    path.chmod(0o600)
    with pytest.raises(LibraryError):
        update_skills(settings, upload())
    db = sqlite3.connect(path)
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == [("keep_me",)]
    db.close()


def test_atomic_rollback_on_audit_failure_and_quota(settings, monkeypatch):
    from jx_alert import admin_skill_library as library

    update_skills(settings, upload())
    root = Path(settings.agent_skill_library_dir)
    db = sqlite3.connect(root / "library.sqlite3")
    db.execute("CREATE TRIGGER simulate_write_failure BEFORE INSERT ON skill_audit BEGIN SELECT RAISE(ABORT,'stop'); END;")
    db.close()
    with pytest.raises(LibraryError):
        update_skills(settings, upload(TEXT.replace("efficiency", "reach"), 1))
    assert list_skills(settings)["revision"] == 1
    assert len(inspect_skill(settings, "ad-advice")["versions"]) == 1
    monkeypatch.setattr(library, "MAX_VERSIONS", 1)
    with pytest.raises(LibraryError, match="数量已达上限"):
        update_skills(settings, upload(TEXT.replace("efficiency", "reach"), 1))
    monkeypatch.setattr(library, "MAX_SKILLS", 1)
    with pytest.raises(LibraryError, match="数量已达上限"):
        update_skills(settings, upload(TEXT.replace("name: ad-advice", "name: other"), 1))


def test_scalar_yaml_front_matter_and_explicit_compatibility():
    sample = """---
name: review-2026
description: "单文件技能"
license: MIT
compatibility: "仅文档，不保证支持所有模型。"
metadata: {author: sample, version: "1.0"}
---
Body
"""
    data = upload(sample)
    doc = parse_upload(data["filename"], data["content_base64"])
    assert doc.name == "review-2026" and doc.objective is None
