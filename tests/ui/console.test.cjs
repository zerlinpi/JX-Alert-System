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

test("store search matches names, sites, profile IDs and operators", () => {
  const rows = [{ name: "香港川页", short_name: "香港川页", country_label: "美国", profile_id: "101",
                  operators: [{ name: "皮泽霖" }] },
                { name: "Campx-主账号", short_name: "Campx", country_label: "美国", profile_id: "102", operators: [] }];
  assert.equal(consoleUi.filterStores(rows, "皮泽").length, 1);
  assert.equal(consoleUi.filterStores(rows, "campx")[0].profile_id, "102");
  assert.equal(consoleUi.filterStores(rows, "").length, 2);
});

test("lane form values are validated before saving", () => {
  const values = {
    daily: { enabled: true, to_group: false, to_lead: true, ai: true },
    review: { enabled: true, to_group: false, to_lead: true, to_operators: true, ai: false, weekly: true },
    hourly: { enabled: true, to_group: false, to_lead: true, to_operators: true, start_hour: "9", end_hour: "24",
              high_spend: "30", low_spend: "10", min_clicks: "15", acos_high: "60", cvr_low: "5", acos_good: "20",
              min_orders_good: "2" },
  };
  const lanes = consoleUi.lanePayload(values);
  assert.equal(lanes.hourly.start_hour, 9);
  assert.equal(lanes.review.weekly, true);
  assert.throws(() => consoleUi.lanePayload({ ...values, hourly: { ...values.hourly, low_spend: "40" } }), /低花费/);
  assert.throws(() => consoleUi.lanePayload({ ...values, hourly: { ...values.hourly, start_hour: "23", end_hour: "9" } }), /时段/);
  assert.throws(() => consoleUi.lanePayload({ ...values, hourly: { ...values.hourly, min_clicks: "" } }), /阈值/);
});
