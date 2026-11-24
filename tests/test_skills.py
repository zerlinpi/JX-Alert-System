"""Installing the official read-only SparkX Skills and reading them through read_skill."""
import io
import json
import tarfile

import httpx
import pytest

from jx_alert.sparkx import skills

SHA = "b72b914b891ad9ed357dd0a36ab0da53152aae85"
MANIFEST = {"release": "v1.3.7", "updated_at": "2026-11-18", "skills": [
    {"name": "sparkx-query-ads-performance", "version": "1.4.11", "required": True, "mcp_tools": ["get_ads_perf"],
     "path": "sparkx-query-ads-performance", "description": "Query advertising performance metrics."},
    {"name": "sparkx-weekly-ads-report", "version": "1.1.6", "required": False,
     "mcp_tools": ["get_ads_perf", "get_entity_metadata", "get_operation_log"], "path": "sparkx-weekly-ads-report",
     "description": "Weekly recap."},
    {"name": "sparkx-edit-ads", "version": "1.2.0", "required": True, "mcp_tools": ["batch_update_ads"],
     "path": "sparkx-edit-ads", "description": "Edit live ad entities."},
    {"name": "sparkx-promo-prep", "version": "1.0.0", "required": False,
     "mcp_tools": ["get_ads_perf", "batch_update_ads"], "path": "sparkx-promo-prep", "description": "Promo."}]}


def archive(extra=None, manifest=MANIFEST):
    files = {
        "skills/manifest.json": json.dumps(manifest),
        "skills/sparkx-query-ads-performance/SKILL.md": "# Query Ads Performance Skill\n" + "规则" * 30000,
        "skills/sparkx-query-ads-performance/references/platform-notes.md": "Ratio Metric Display Rule",
        "skills/sparkx-weekly-ads-report/SKILL.md": "# Weekly report",
        "skills/sparkx-edit-ads/SKILL.md": "# Edit ads (write)",
        "skills/sparkx-promo-prep/SKILL.md": "# Promo (write)",
        "skills/sparkx-query-ads-performance/../../../escape.md": "outside",
        "README.md": "repo readme",
        **(extra or {})}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": SHA}) as bundle:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo("SparkXAI-MCP-main/" + name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def transport(body, status=200):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status, content=body)
    mock = httpx.MockTransport(handler)
    mock.seen = seen
    return mock


def test_only_read_only_skills_are_installed_and_readable(settings):
    mock = transport(archive())
    index = skills.install(settings, transport=mock)
    assert mock.seen == ["https://codeload.github.com/SparkXAI/SparkXAI-MCP/tar.gz/main"]
    assert [entry["name"] for entry in index["skills"]] == ["sparkx-query-ads-performance", "sparkx-weekly-ads-report"]
    assert index["release"] == "v1.3.7" and index["commit"] == SHA
    assert skills.installed(settings)["folder"] == index["folder"]
    first = skills.read(settings, index, "sparkx-query-ads-performance")
    assert first.startswith("[sparkx-query-ads-performance 1.4.11 · SKILL.md · 第 1–40000 字符") and "offset=40000" in first
    rest = skills.read(settings, index, "sparkx-query-ads-performance", "SKILL.md", 40000)
    assert "未读完" not in rest
    assert "Ratio Metric" in skills.read(settings, index, "sparkx-query-ads-performance", "references/platform-notes.md")
    assert "没有文件" in skills.read(settings, index, "sparkx-query-ads-performance", "../../escape.md")
    assert "没有这个 Skill" in skills.read(settings, index, "sparkx-edit-ads")
    text = skills.prompt_index(index)
    assert "sparkx-weekly-ads-report 1.1.6" in text and "references/platform-notes.md" in text and "edit" not in text


def test_reinstall_switches_folders_and_failures_keep_the_old_install(settings):
    first = skills.install(settings, transport=transport(archive()))
    second = skills.install(settings, transport=transport(archive()))
    root = skills._root(settings)
    assert first["folder"] != second["folder"] and not (root / first["folder"]).exists()
    with pytest.raises(skills.SkillsError):
        skills.install(settings, transport=transport(b"", status=404))
    with pytest.raises(skills.SkillsError):
        skills.install(settings, transport=transport(b"not a tarball"))
    with pytest.raises(skills.SkillsError):
        skills.install(settings, transport=transport(archive(manifest={"release": "x", "skills": [
            {"name": "sparkx-edit-ads", "path": "sparkx-edit-ads", "mcp_tools": ["batch_update_ads"]}]})))
    assert skills.installed(settings)["folder"] == second["folder"]


def test_status_reports_outdated_skills(settings):
    skills.install(settings, transport=transport(archive()))
    newer = {**MANIFEST, "release": "v1.3.8", "skills": [{**MANIFEST["skills"][0], "version": "1.4.12"},
                                                           MANIFEST["skills"][1]]}
    view = skills.status(settings, check=True, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=newer)))
    assert view["installed"]["release"] == "v1.3.7" and view["latest"]["outdated"] == ["sparkx-query-ads-performance"]
    assert [row["name"] for row in view["skills"]] == ["sparkx-query-ads-performance", "sparkx-weekly-ads-report"]


def test_auto_install_is_attempted_once_per_interval(settings, monkeypatch):
    attempts = []
    monkeypatch.setattr(skills, "_auto_attempt", {"at": 0.0})

    def failing(settings_, **kwargs):
        attempts.append(1)
        raise skills.SkillsError("offline")

    monkeypatch.setattr(skills, "install", failing)
    assert skills.ensure(settings) is None and skills.ensure(settings) is None
    assert attempts == [1]
