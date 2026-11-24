"use strict";
// Browser-level smoke test: the real console HTML/JS in jsdom against a mocked API.
// Run: npm install --no-save jsdom@24 && node tests/ui/console.smoke.cjs
const fs = require("fs");
const path = require("path");
let JSDOM;
try { ({ JSDOM } = require("jsdom")); } catch (error) {
  console.log("jsdom is not installed; skipped (npm install --no-save jsdom@24)");
  process.exit(0);
}

const root = process.argv[2] || path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "src/jx_alert/admin_ui/index.html"), "utf8").replace("{{CSRF}}", "a".repeat(64));
const script = fs.readFileSync(path.join(root, "src/jx_alert/admin_ui/console.js"), "utf8");
const errors = [];
const requests = [];
const dom = new JSDOM(html, { url: "https://jx.example/console/", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.addEventListener("error", (event) => errors.push(String(event.error || event.message)));
window.console.error = (...args) => errors.push(args.join(" "));
window.HTMLElement.prototype.scrollIntoView = function () {};

const card = { schema: "2.0", header: { template: "orange", title: { content: "SparkX 广告日报 · 2026-11-22" } },
  body: { elements: [{ tag: "markdown", content: "**全公司核心指标**\n第二行" },
    { tag: "table", columns: [{ name: "c0", display_name: "指标" }, { name: "c1", display_name: "当日" }],
      rows: [{ c0: "广告花费", c1: "$4,034" }] }, { tag: "hr" }] } };
const lanes = { daily: { enabled: false, to_group: false, to_lead: true, ai: true },
  review: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, ai: true, weekly: false },
  hourly: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, start_hour: 9, end_hour: 24,
            high_spend: 30, low_spend: 10, min_clicks: 15, acos_high: 60, cvr_low: 5, acos_good: 20, min_orders_good: 2 } };
const responses = {
  "GET /console/api/overview": { sparkx: { configured: true, token_source: "console" }, deepseek: { configured: false },
    feishu: { delivery_enabled: true, group: "日报群", bot_enabled: true }, stores: { authorized: 7, monitored: 6, mapped: 2,
    roster_at: "2026-11-24T00:30:00Z" }, lanes: { daily: { enabled: true, last: { status: "SUCCEEDED", at: "2026-11-24T00:31:00Z",
    queued: 1, narrative: "deepseek" } }, review: { enabled: false, last: null }, hourly: { enabled: false, last: null } },
    recent: [{ created_at: "2026-11-24T00:31:00Z", lane: "08:30 日报", recipient: "负责人：老板", status: "DELIVERED" }], csrf: "a".repeat(64) },
  "GET /console/api/stores": { revision: "0".repeat(64), roster_at: "2026-11-24T00:30:00Z", rows: [
    { profile_id: "101", name: "香港川页", short_name: "香港川页", country_label: "美国", store_type: "seller", authorized: true,
      monitored: true, operators: [{ id: "ou_op1", name: "皮泽霖" }] },
    { profile_id: "102", name: "Campx-主账号", short_name: "Campx", country_label: "美国", store_type: "seller", authorized: true,
      monitored: true, operators: [] }] },
  "GET /console/api/members": { members: [{ id: "ou_op1", name: "皮泽霖" }, { id: "ou_op2", name: "高爽" }] },
  "POST /console/api/stores/save": null,
  "GET /console/api/recipients": { revision: "1".repeat(64), group: "日报群", company: { id: "ou_lead", name: "老板" }, ad: null,
    group_detail: { name: "日报群" } },
  "GET /console/api/reports": { revision: "2".repeat(64), updated_at: null, lanes },
  "POST /console/api/reports": { revision: "3".repeat(64), updated_at: "2026-11-24T01:00:00Z", lanes },
  "POST /console/api/reports/preview": { card, text: "SparkX 广告日报", info: { profiles: 6, report_date: "2026-11-22", narrative: "rules" } },
  "GET /console/api/groups": { chat_id: "oc_reports", name: "日报群", revision: "1".repeat(64), chats: [{ id: "oc_reports", name: "日报群" }] },
  "GET /console/api/sparkx": { revision: "0".repeat(64), configured: true, token_source: "console", endpoint: "https://mcp.sparkx.cn/mcp" },
  "POST /console/api/sparkx/test": { status: "OK", profile_count: 1, profiles: [{ profile_id: "101", name: "香港川页", country: "US", store_type: "seller" }] },
  "GET /console/api/deepseek": { revision: "0".repeat(64), configured: false, models: ["deepseek-flash", "deepseek-v4-pro"], model: "deepseek-flash", timeout: 45 },
  "POST /console/api/deepseek/test": { status: "NOT_CONFIGURED" },
  "GET /console/api/deliveries": { rows: [{ created_at: "2026-11-24T00:31:00Z", lane: "08:30 日报", recipient: "负责人：老板",
    title: "SparkX 广告日报", status: "DELIVERY_UNKNOWN", error: "可能已提交飞书", delivered_at: null }] },
};
window.fetch = async (url, init = {}) => {
  const key = `${init.method || "GET"} ${url}`;
  requests.push({ key, headers: init.headers || {}, body: init.body });
  let data = responses[key];
  if (key === "POST /console/api/stores/save") {
    const body = JSON.parse(init.body);
    data = JSON.parse(JSON.stringify(responses["GET /console/api/stores"]));
    data.rows[1].operators = body.changes["102"] ? body.changes["102"].operator_ids.map((id) => ({ id, name: "高爽" })) : [];
  }
  if (data === undefined) return { status: 404, ok: false, json: async () => ({ error: "未模拟 " + key }) };
  return { status: 200, ok: true, json: async () => data };
};
window.eval(script);
const tick = () => new Promise((resolve) => setTimeout(resolve, 20));
const $ = (id) => window.document.getElementById(id);
const click = (node) => node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

(async () => {
  window.JXConsole.start(window.document, window);
  await tick();
  const checks = [];
  checks.push(["overview cards", $("status-cards").children.length === 4]);
  checks.push(["lane rows", $("lane-rows").children.length === 3]);
  checks.push(["recent rows", $("recent-rows").textContent.includes("已送达")]);
  click(window.document.querySelector('[data-view="stores"]'));
  await tick();
  checks.push(["stores panel visible", !window.document.querySelector('[data-panel="stores"]').hidden]);
  checks.push(["store rows", $("store-rows").children.length === 2 && $("store-rows").textContent.includes("未映射运营")]);
  checks.push(["five store columns", window.document.querySelectorAll(".stores-table thead th").length === 5
    && [...$("store-rows").children].every((tr) => tr.children.length === 5)]);
  click($("store-rows").children[1].querySelector("button"));
  await tick();
  checks.push(["picker open", !$("operator-picker").hidden && $("picker-members").querySelectorAll("input").length === 2]);
  $("picker-members").querySelectorAll("input")[1].click();
  click($("picker-save"));
  await tick();
  const save = requests.find((request) => request.key === "POST /console/api/stores/save");
  checks.push(["save sends csrf + operator", save && save.headers["X-CSRF-Token"] === "a".repeat(64)
    && JSON.parse(save.body).changes["102"].operator_ids[0] === "ou_op2"]);
  checks.push(["picker closed and chip updated", $("operator-picker").hidden && $("store-rows").textContent.includes("高爽")]);
  click(window.document.querySelector('[data-view="notifications"]'));
  await tick();
  checks.push(["recipients", $("lead-company").textContent === "老板" && $("lead-ad").textContent === "未设置"]);
  const review = window.document.querySelector('#lanes-form [data-lane="review"] input[name="enabled"]');
  checks.push(["lane form filled", review.checked && window.document.querySelector('#lanes-form [name="high_spend"]').value === "30"]);
  window.document.querySelector('#lanes-form [data-lane="daily"] input[name="enabled"]').checked = true;
  $("lanes-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await tick();
  const saved = requests.find((request) => request.key === "POST /console/api/reports");
  checks.push(["lanes saved", saved && JSON.parse(saved.body).lanes.daily.enabled === true
    && JSON.parse(saved.body).lanes.hourly.min_clicks === 15 && JSON.parse(saved.body).lanes.review.mention_operators === true]);
  checks.push(["mention switch shown", window.document.querySelectorAll('#lanes-form input[name="mention_operators"]').length === 2]);
  click(window.document.querySelector('[data-preview="daily"]'));
  await tick();
  checks.push(["preview rendered", !$("preview-panel").hidden && $("preview-card").querySelector("table") !== null
    && $("preview-card").querySelector("strong").textContent === "全公司核心指标"]);
  click(window.document.querySelector('[data-lead="ad"]'));
  await tick();
  checks.push(["lead picker", !$("lead-picker").hidden && $("lead-members").querySelectorAll('input[type="radio"]').length === 2]);
  click($("load-groups"));
  await tick();
  checks.push(["groups loaded", !$("group-select").disabled && $("group-select").options.length === 1]);
  click(window.document.querySelector('[data-view="sparkx"]'));
  await tick();
  checks.push(["sparkx facts", $("sparkx-facts").textContent.includes("已保存（控制台）")]);
  click($("sparkx-test"));
  await tick();
  checks.push(["sparkx test", !$("sparkx-profiles-panel").hidden && $("sparkx-status").textContent.includes("授权 1 家")]);
  click(window.document.querySelector('[data-view="deepseek"]'));
  await tick();
  checks.push(["deepseek models", $("deepseek-model").options.length === 2]);
  click($("deepseek-test"));
  await tick();
  checks.push(["deepseek test", $("deepseek-status").textContent === "尚未配置密钥。"]);
  click(window.document.querySelector('[data-view="deliveries"]'));
  await tick();
  checks.push(["deliveries", $("delivery-rows").textContent.includes("可能已提交（不自动重发）")]);
  const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
  console.log(JSON.stringify({ passed: checks.length - failed.length, failed, errors }, null, 1));
  process.exit(failed.length || errors.length ? 1 : 0);
})();
