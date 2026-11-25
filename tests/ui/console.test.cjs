"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const consoleUi = require("../../src/jx_alert/admin_ui/console.js");

test("outbox statuses read as plain Chinese and never claim an unknown delivery", () => {
  assert.deepEqual(consoleUi.statusLabel("DELIVERED"), { text: "已送达", tone: "ok" });
  assert.equal(consoleUi.statusLabel("DELIVERY_UNKNOWN").text, "可能已提交（不自动重发）");
  assert.equal(consoleUi.statusLabel("SOMETHING_NEW").text, "SOMETHING_NEW");
  assert.equal(consoleUi.runLabel(undefined).text, "尚未运行");
});

test("times are shown in Beijing time", () => {
  assert.equal(consoleUi.formatTime("2026-11-24T00:30:00Z"), "11/24 08:30");
  assert.equal(consoleUi.formatTime(null), "—");
  assert.equal(consoleUi.formatTime("not a date"), "—");
});

test("bold markers become separate segments without interpreting any markup", () => {
  assert.deepEqual(consoleUi.parseBold("**一、最严重**：<at id=ou_x>"), [
    { text: "一、最严重", bold: true }, { text: "：<at id=ou_x>", bold: false }]);
});

test("card text mirrors the server plain-text rendering", () => {
  const card = { header: { title: { content: "SparkX 广告日报" } }, body: { elements: [
    { tag: "markdown", content: "统计日" },
    { tag: "table", columns: [{ name: "c0", display_name: "店铺" }, { name: "c1", display_name: "花费" }],
      rows: [{ c0: "川页", c1: "3,217.70" }] },
    { tag: "hr" }] } };
  assert.equal(consoleUi.cardText(card), "SparkX 广告日报\n统计日\n店铺 | 花费\n川页 | 3,217.70\n————————");
});

const STORES = [
  { name: "香港川页", short_name: "香港川页", country_label: "美国", profile_id: "101", monitored: true, operators: [{ name: "皮泽霖" }] },
  { name: "Campx-主账号", short_name: "Campx", country_label: "美国", profile_id: "102", monitored: true, operators: [] },
  { name: "昌泉", short_name: "昌泉", country_label: "美国", profile_id: "105", monitored: false, operators: [] },
];

test("store search and filters match names, sites, profile IDs, operators and states", () => {
  assert.equal(consoleUi.filterStores(STORES, "皮泽").length, 1);
  assert.equal(consoleUi.filterStores(STORES, "campx")[0].profile_id, "102");
  assert.equal(consoleUi.filterStores(STORES, "").length, 3);
  assert.deepEqual(consoleUi.filterStores(STORES, "", "unmapped").map((row) => row.profile_id), ["102"]);
  assert.deepEqual(consoleUi.filterStores(STORES, "", "paused").map((row) => row.profile_id), ["105"]);
  assert.deepEqual(consoleUi.storeCounts(STORES), { all: 3, unmapped: 1, paused: 1 });
});

test("stores can be narrowed to one site, for example to pause the sites without data", () => {
  const rows = [...STORES, { name: "川页-CA", short_name: "川页-CA", country_label: "加拿大", profile_id: "201", monitored: true, operators: [] },
    { name: "昌泉-CA", short_name: "昌泉-CA", country_label: "加拿大", profile_id: "202", monitored: true, operators: [] }];
  assert.deepEqual(consoleUi.siteOptions(rows), [{ label: "美国", count: 3 }, { label: "加拿大", count: 2 }]);
  assert.deepEqual(consoleUi.filterStores(rows, "", "all", "加拿大").map((row) => row.profile_id), ["201", "202"]);
  assert.deepEqual(consoleUi.filterStores(rows, "昌泉", "all", "加拿大").map((row) => row.profile_id), ["202"]);
  assert.deepEqual(consoleUi.storeCounts(rows, "加拿大"), { all: 2, unmapped: 2, paused: 0 });
});

test("token counts, preview states and mention lists read naturally", () => {
  assert.equal(consoleUi.formatTokens(180573), "18.1 万");
  assert.equal(consoleUi.formatTokens(9000), "9000");
  assert.equal(consoleUi.formatTokens(undefined), "");
  const started = "2026-11-24T06:00:00Z";
  const running = consoleUi.previewStateText({ status: "running", started_at: started }, Date.parse(started) + 125000);
  assert.equal(running.tone, "running");
  assert.match(running.text, /预览生成中（已用 2 分 5 秒）/);
  assert.match(consoleUi.previewStateText({ status: "done", finished_at: "2026-11-24T06:03:00Z" }).text, /已生成预览 · 11\/24 14:03/);
  assert.equal(consoleUi.previewStateText({ status: "failed" }).tone, "failed");
  assert.equal(consoleUi.previewStateText(undefined).text, "");
  assert.equal(consoleUi.mentionText([{ name: "高爽", stores: ["Campx", "XPE"] }, { name: "皮泽霖", stores: ["香港川页"] }]),
    "高爽（Campx、XPE）、皮泽霖（香港川页）");
});

test("an unchanged default task text is saved as empty so improved defaults still apply", () => {
  assert.equal(consoleUi.promptValue("  默认说明\n", "默认说明"), "");
  assert.equal(consoleUi.promptValue("只看 ACOS", "默认说明"), "只看 ACOS");
  assert.equal(consoleUi.promptValue("", ""), "");
});

test("lane form values are validated before saving", () => {
  const values = {
    daily: { enabled: true, to_group: false, mention_operators: true, to_lead: true, prompt: "日报默认" },
    review: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, weekly: true, prompt: "多看 ACOS" },
    hourly: { enabled: true, to_group: true, mention_operators: false, to_lead: true, to_operators: false, start_hour: "9", end_hour: "24",
              high_spend: "30", low_spend: "10", min_clicks: "15", acos_high: "60", cvr_low: "5", acos_good: "20",
              min_orders_good: "2", prompt: "" },
  };
  const lanes = consoleUi.lanePayload(values, { daily: "日报默认", review: "复盘默认", hourly: "" });
  assert.equal(lanes.hourly.start_hour, 9);
  assert.equal(lanes.review.weekly, true);
  assert.equal(lanes.daily.prompt, "");
  assert.equal(lanes.review.prompt, "多看 ACOS");
  assert.equal(lanes.hourly.mention_operators, false);
  assert.equal(lanes.daily.mention_operators, true);
  assert.equal("ai" in lanes.daily, false);
  assert.throws(() => consoleUi.lanePayload({ ...values, hourly: { ...values.hourly, low_spend: "40" } }), /低花费/);
  assert.throws(() => consoleUi.lanePayload({ ...values, hourly: { ...values.hourly, start_hour: "23", end_hour: "9" } }), /时段/);
  assert.throws(() => consoleUi.lanePayload({ ...values, hourly: { ...values.hourly, min_clicks: "" } }), /阈值/);
  assert.throws(() => consoleUi.lanePayload({ ...values, daily: { ...values.daily, prompt: "字".repeat(4001) } }), /4000/);
});

test("the setup checklist follows the overview", () => {
  const none = consoleUi.setupSteps({});
  assert.deepEqual(none.map((step) => step.key), ["sparkx", "skills", "deepseek", "recipients", "operators", "delivery"]);
  assert.equal(none.every((step) => !step.done), true);
  const ready = consoleUi.setupSteps({ sparkx: { configured: true }, deepseek: { configured: true }, skills: { installed: true },
    feishu: { group: "日报群", company_lead: true, ad_lead: true, delivery_enabled: true, scheduler_enabled: true },
    stores: { mapped: 0 } });
  assert.deepEqual(ready.filter((step) => !step.done).map((step) => step.key), ["operators"]);
  assert.equal(ready.find((step) => step.key === "operators").optional, true);
});

test("leads are lists of names, also for an older server's single lead", () => {
  assert.equal(consoleUi.leadNames([{ id: "ou_a", name: "老板" }, { id: "ou_b", name: "高爽" }]), "老板、高爽");
  assert.equal(consoleUi.leadNames({ id: "ou_a", name: "老板" }), "老板");
  assert.equal(consoleUi.leadNames(null), "");
  assert.deepEqual(consoleUi.leadList([]), []);
});

test("skill states say why a skill is or is not used", () => {
  assert.deepEqual(consoleUi.skillState({ state: "on", tools: [] }), { text: "使用中", tone: "ok", detail: "" });
  assert.equal(consoleUi.skillState({ state: "missing_tools", tools: ["get_market_search_perf"] }).detail,
    "当前 SparkX Token 没有提供：get_market_search_perf");
  assert.deepEqual(consoleUi.skillState({ state: "write_off", tools: ["batch_update_ads"] }),
    { text: "写入未开启", tone: "", detail: "开启「广告写入」后启用" });
  assert.deepEqual(consoleUi.skillState({ state: "unsupported", tools: ["list_skills"] }),
    { text: "不支持，不安装", tone: "bad", detail: "需要本系统不提供的工具：list_skills" });
  assert.equal(consoleUi.skillState({ state: "off" }).text, "已停用");
});

test("write attempts never read as done unless SparkX confirmed them", () => {
  assert.deepEqual(consoleUi.writeLabel("EXECUTED"), { text: "已执行", tone: "ok" });
  assert.deepEqual(consoleUi.writeLabel("RUNNING"), { text: "已提交，结果未知", tone: "warn" });
  assert.equal(consoleUi.writeLabel("BLOCKED").text, "已拦截，未执行");
  assert.equal(consoleUi.writeLabel("PREVIEW").text, "预览（待二次确认）");
  assert.equal(consoleUi.writeLabel("SOMETHING_NEW").text, "SOMETHING_NEW");
});

test("file sizes read naturally", () => {
  assert.equal(consoleUi.fileSize(512), "512 B");
  assert.equal(consoleUi.fileSize(12595), "12.3 KB");
  assert.equal(consoleUi.fileSize(3 * 1024 * 1024), "3.0 MB");
  assert.equal(consoleUi.fileSize(undefined), "—");
});