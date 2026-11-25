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
     "mcp_tools": ["get_ads_perf", "batch_update_ads"], "path": "sparkx-promo-prep", "description": "Promo."},
    {"name": "sparkx-query-market-data", "version": "1.0.1", "required": False,
     "mcp_tools": ["get_market_search_perf", "get_peer_benchmark"], "path": "sparkx-query-market-data",
     "description": "SQP / ABA search terms and peer benchmarks."}]}


def archive(extra=None, manifest=MANIFEST):
    files = {
        "skills/manifest.json": json.dumps(manifest),
        "skills/sparkx-query-ads-performance/SKILL.md": "# Query Ads Performance Skill\n" + "规则" * 30000,
        "skills/sparkx-query-ads-performance/references/platform-notes.md": "Ratio Metric Display Rule",
        "skills/sparkx-weekly-ads-report/SKILL.md": "# Weekly report",
        "skills/sparkx-edit-ads/SKILL.md": "# Edit ads (write)",
        "skills/sparkx-promo-prep/SKILL.md": "# Promo (write)",
        "skills/sparkx-query-market-data/SKILL.md": "# Query Market and Peer Data",
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


def install_fake(settings):
    """The official repository stand-in installed: three read-only Skills, two that need write tools."""
    return skills.install(settings, transport=transport(archive()))


def test_only_read_only_skills_are_installed_and_readable(settings):
    mock = transport(archive())
    index = skills.install(settings, transport=mock)
    assert mock.seen == ["https://codeload.github.com/SparkXAI/SparkXAI-MCP/tar.gz/main"]
    assert [entry["name"] for entry in index["skills"]] == ["sparkx-query-ads-performance", "sparkx-weekly-ads-report",
                                                            "sparkx-query-market-data"]
    # The whole official list is kept, so the console can say why a Skill is not installed.
    blocked = {entry["name"]: entry["blocked_tools"] for entry in index["catalog"] if not entry["installable"]}
    assert blocked == {"sparkx-edit-ads": ["batch_update_ads"], "sparkx-promo-prep": ["batch_update_ads"]}
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
    assert [row["name"] for row in view["skills"]] == ["sparkx-query-ads-performance", "sparkx-weekly-ads-report",
                                                       "sparkx-query-market-data"]


def test_optional_skills_can_be_switched_off_but_required_ones_stay_on(settings, monkeypatch):
    monkeypatch.setattr(skills, "_auto_attempt", {"at": 0.0})
    install_fake(settings)
    current = skills.choices(settings)
    assert current["disabled"] == [] and current["revision"] == "0" * 64
    skills.save_choices(settings, ["sparkx-weekly-ads-report"], current["revision"])
    agent_index = skills.for_agent(settings)
    assert [entry["name"] for entry in agent_index["skills"]] == ["sparkx-query-ads-performance",
                                                                  "sparkx-query-market-data"]
    assert "没有这个 Skill" in skills.read(settings, agent_index, "sparkx-weekly-ads-report")
    with pytest.raises(skills.SkillsError) as exc:
        skills.save_choices(settings, ["sparkx-query-ads-performance"], skills.choices(settings)["revision"])
    assert exc.value.status_code == 400 and "基础查询" in exc.value.detail
    with pytest.raises(skills.SkillsError) as exc:
        skills.save_choices(settings, ["sparkx-edit-ads"], skills.choices(settings)["revision"])  # never installed
    assert exc.value.status_code == 400
    with pytest.raises(skills.SkillsError) as exc:
        skills.save_choices(settings, [], "1" * 64)
    assert exc.value.status_code == 409
    skills.save_choices(settings, [], skills.choices(settings)["revision"])
    assert len(skills.for_agent(settings)["skills"]) == 3
    states = {row["name"]: (row["state"], row["tools"]) for row in skills.status(
        settings, offered=["get_ads_perf", "get_entity_metadata", "get_operation_log"])["catalog"]}
    assert states["sparkx-query-market-data"] == ("missing_tools", ["get_market_search_perf", "get_peer_benchmark"])
    assert states["sparkx-promo-prep"] == ("write", ["batch_update_ads"]) and states["sparkx-weekly-ads-report"][0] == "on"


def test_a_run_offers_only_skills_whose_tools_this_token_gets(settings, monkeypatch):
    """Market-data Skills need extra SparkX scopes: without those tools DeepSeek is not pointed at them."""
    import anyio

    from jx_alert.ai import agent
    from jx_alert.sparkx import stores
    from sparkx_fake import FakeSparkX, ScriptedDeepSeek, install, reply

    index = install_fake(settings)
    assert [entry["name"] for entry in skills.usable(index, [])["skills"]] == [entry["name"] for entry in index["skills"]]
    install(monkeypatch, FakeSparkX(), agent)
    client = ScriptedDeepSeek([reply("好的。")])
    seen = {}

    def messages_for(context):
        seen["skills"] = [entry["name"] for entry in context["skills"]["skills"]]
        return [{"role": "system", "content": skills.prompt_index(context["skills"])}, {"role": "user", "content": "x"}]

    async def conversation():
        return await agent.run_task(settings, client, token="synthetic-sparkx-token", messages_for=messages_for,
                                    now=None, directory=stores.load(settings), need_roster=False, max_tool_calls=2,
                                    max_rounds=2, skills_index=index)

    anyio.run(conversation)
    assert seen["skills"] == ["sparkx-query-ads-performance", "sparkx-weekly-ads-report"]
    assert "sparkx-query-market-data" not in client.requests[0]["messages"][0]["content"]


def test_auto_install_is_attempted_once_per_interval(settings, monkeypatch):
    attempts = []
    monkeypatch.setattr(skills, "_auto_attempt", {"at": 0.0})

    def failing(settings_, **kwargs):
        attempts.append(1)
        raise skills.SkillsError("offline")

    monkeypatch.setattr(skills, "install", failing)
    assert skills.ensure(settings) is None and skills.ensure(settings) is None
    assert attempts == [1]
