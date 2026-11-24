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
window.JX_PREVIEW_INTERVAL = 10;
const card = { schema: "2.0", header: { template: "blue", title: { content: "SparkX 广告日报 · 2026-11-22" } },
  body: { elements: [{ tag: "markdown", content: "**全公司核心指标**\n第二行" },
    { tag: "table", columns: [{ name: "c0", display_name: "指标" }, { name: "c1", display_name: "当日" }],
      rows: [{ c0: "广告花费", c1: "$4,034" }] }, { tag: "hr" }] } };
const defaults = { daily: "日报默认说明", review: "复盘默认说明", hourly: "" };
const lanes = { daily: { enabled: true, to_group: true, to_lead: true, prompt: "" },
  review: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, weekly: false, prompt: "" },
  hourly: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, start_hour: 9, end_hour: 24,
            high_spend: 30, low_spend: 10, min_clicks: 15, acos_high: 60, cvr_low: 5, acos_good: 20, min_orders_good: 2, prompt: "" } };
const storeRows = () => [
  { profile_id: "101", name: "香港川页", short_name: "香港川页", country_label: "美国", store_type: "seller", authorized: true,
    monitored: true, operators: [{ id: "ou_op1", name: "皮泽霖" }] },
  { profile_id: "102", name: "Campx-主账号", short_name: "Campx", country_label: "美国", store_type: "seller", authorized: true,
    monitored: true, operators: [] },
  { profile_id: "105", name: "昌泉", short_name: "昌泉", country_label: "美国", store_type: "seller", authorized: true,
    monitored: false, operators: [] }];
let polls = 0;
const responses = {
  "GET /console/api/overview": () => ({ sparkx: { configured: true, token_source: "console" }, deepseek: { configured: true, model: "deepseek-v4-pro" },
    skills: { installed: false, release: null, count: 0 },
    feishu: { delivery_enabled: true, scheduler_enabled: true, group: "日报群", company_lead: false, ad_lead: true, bot_enabled: true },
    stores: { authorized: 3, monitored: 2, mapped: 1, roster_at: "2026-11-24T00:30:00Z" },
    lanes: { daily: { enabled: true, last: { status: "SUCCEEDED", at: "2026-11-24T00:31:00Z", queued: 2, sparkx_calls: 6 } },
             review: { enabled: true, last: null }, hourly: { enabled: false, last: null, start_hour: 9, end_hour: 24 } },
    recent: [{ created_at: "2026-11-24T00:31:00Z", lane: "08:30 日报", recipient: "负责人：老板", status: "DELIVERED" }], csrf: "a".repeat(64) }),
  "GET /console/api/stores": () => ({ revision: "0".repeat(64), roster_at: "2026-11-24T00:30:00Z", rows: storeRows() }),
  "GET /console/api/members": () => ({ members: [{ id: "ou_op1", name: "皮泽霖" }, { id: "ou_op2", name: "高爽" }] }),
  "POST /console/api/stores/save": (body) => {
    const rows = storeRows();
    for (const item of rows) {
      const change = body.changes[item.profile_id];
      if (change) { item.monitored = change.monitored; item.operators = change.operator_ids.map((id) => ({ id, name: id === "ou_op2" ? "高爽" : "皮泽霖" })); }
    }
    return { revision: "9".repeat(64), roster_at: "2026-11-24T00:30:00Z", rows };
  },
  "GET /console/api/recipients": () => ({ revision: "1".repeat(64), group: "日报群", company: { id: "ou_lead", name: "老板" }, ad: null,
    group_detail: { name: "日报群" } }),
  "GET /console/api/reports": () => ({ revision: "2".repeat(64), updated_at: null, lanes, default_prompts: defaults, max_prompt: 4000 }),
  "POST /console/api/reports": (body) => ({ revision: "3".repeat(64), updated_at: "2026-11-24T01:00:00Z", lanes: body.lanes, default_prompts: defaults }),
  "POST /console/api/reports/preview": (body) => ({ job: "job-1", lane: body.lane }),
  "GET /console/api/reports/preview?job=job-1": () => (++polls < 2 ? { status: "running" }
    : { status: "done", result: { cards: [card], text: "SparkX 广告日报", info: { profiles: 2, report_date: "2026-11-22", sparkx_calls: 5, seconds: 70 } } }),
  "GET /console/api/groups": () => ({ chat_id: "oc_reports", name: "日报群", revision: "1".repeat(64), chats: [{ id: "oc_reports", name: "日报群" }] }),
  "GET /console/api/sparkx": () => ({ revision: "0".repeat(64), configured: true, token_source: "console", endpoint: "https://mcp.sparkx.cn/mcp" }),
  "GET /console/api/skills": () => ({ installed: { release: "v1.3.7", commit: "b72b914b8", installed_at: "2026-11-24T02:00:00Z" },
    skills: [{ name: "sparkx-query-ads-performance", version: "1.4.11", description: "Query metrics.", mcp_tools: ["get_ads_perf"] }],
    read_tools: ["get_ads_perf", "get_entity_metadata"], source: "SparkXAI/SparkXAI-MCP", latest: null, error: null }),
  "POST /console/api/sparkx/test": () => ({ status: "OK", profile_count: 1, profiles: [{ profile_id: "101", name: "香港川页", country: "US", store_type: "seller" }] }),
  "GET /console/api/deepseek": () => ({ revision: "0".repeat(64), configured: true, models: ["deepseek-flash", "deepseek-v4-pro"],
    model: "deepseek-v4-pro", timeout: 120, thinking: true, key_source: "console" }),
  "POST /console/api/deepseek": (body) => ({ revision: "4".repeat(64), configured: true, models: ["deepseek-flash", "deepseek-v4-pro"],
    model: body.model, timeout: body.timeout, thinking: body.thinking }),
  "POST /console/api/deepseek/test": () => ({ status: "NOT_CONFIGURED" }),
  "GET /console/api/deliveries": () => ({ rows: [{ created_at: "2026-11-24T00:31:00Z", lane: "08:30 日报", recipient: "负责人：老板",
    title: "SparkX 广告日报", status: "DELIVERY_UNKNOWN", error: "可能已提交飞书", delivered_at: null }] }),
};
window.fetch = async (url, init = {}) => {
  const key = `${init.method || "GET"} ${url}`;
  const body = init.body ? JSON.parse(init.body) : undefined;
  requests.push({ key, headers: init.headers || {}, body });
  const handler = responses[key];
  if (!handler) return { status: 404, ok: false, json: async () => ({ error: "未模拟 " + key }) };
  return { status: 200, ok: true, json: async () => handler(body) };
};
window.eval(script);
const tick = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (id) => window.document.getElementById(id);
const doc = window.document;
const click = (node) => node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
const change = (node) => node.dispatchEvent(new window.Event("change", { bubbles: true }));
const input = (node) => node.dispatchEvent(new window.Event("input", { bubbles: true }));
const last = (key) => [...requests].reverse().find((request) => request.key === key);
(async () => {
  window.JXConsole.start(doc, window);
  await tick();
  const checks = [];
  checks.push(["overview title and actions", $("page-title").textContent === "概览"
    && !doc.querySelector('#page-actions [data-for="overview"]').hidden && doc.querySelector('#page-actions [data-for="stores"]').hidden]);
  checks.push(["setup checklist shows the missing steps", !$("setup-panel").hidden && $("setup-steps").children.length === 6
    && $("setup-progress").textContent === "已完成 3/5"]);
  checks.push(["overview stats", $("status-cards").children.length === 4 && $("status-cards").textContent.includes("deepseek-v4-pro")]);
  checks.push(["lane rows", $("lane-rows").children.length === 3 && $("lane-rows").textContent.includes("查询 SparkX 6 次")]);
  checks.push(["recent rows", $("recent-rows").textContent.includes("已送达")]);

  click(doc.querySelector('[data-view="stores"]'));
  await tick();
  checks.push(["stores page", !doc.querySelector('[data-panel="stores"]').hidden && $("page-title").textContent === "店铺与运营"
    && $("store-rows").children.length === 3 && $("store-rows").textContent.includes("未映射运营")]);
  checks.push(["store filter counts", doc.querySelector('[data-count="unmapped"]').textContent === "1"
    && doc.querySelector('[data-count="paused"]').textContent === "1"]);
  click(doc.querySelector('[data-filter="unmapped"]'));
  await tick();
  checks.push(["unmapped filter", $("store-rows").children.length === 1 && $("store-rows").textContent.includes("Campx")]);
  click(doc.querySelector('[data-filter="all"]'));
  await tick();
  const boxes = $("store-rows").querySelectorAll('td.col-check input[type="checkbox"]');
  boxes[0].checked = true; change(boxes[0]);
  boxes[1].checked = true; change(boxes[1]);
  checks.push(["bulk bar", !$("bulk-bar").hidden && $("bulk-count").textContent === "已选 2 家店铺"]);
  click($("bulk-assign"));
  await tick();
  checks.push(["member dialog for two stores", !$("member-dialog").hidden && $("member-dialog-subtitle").textContent === "批量设置 2 家店铺"
    && $("member-list").querySelectorAll('input[type="checkbox"]').length === 2]);
  const op2 = $("member-list").querySelectorAll("input")[1];
  op2.checked = true; change(op2);
  click($("member-save"));
  await tick();
  const bulk = last("POST /console/api/stores/save");
  checks.push(["bulk save sends csrf and both stores", bulk && bulk.headers["X-CSRF-Token"] === "a".repeat(64)
    && JSON.stringify(Object.keys(bulk.body.changes).sort()) === JSON.stringify(["101", "102"])
    && bulk.body.changes["102"].operator_ids[0] === "ou_op2" && bulk.body.changes["101"].monitored === true]);
  checks.push(["dialog closed and selection cleared", $("member-dialog").hidden && $("bulk-bar").hidden && $("store-rows").textContent.includes("高爽")]);
  const monitor = $("store-rows").querySelectorAll('input[role="switch"]')[1];
  monitor.checked = false; change(monitor);
  await tick();
  const single = last("POST /console/api/stores/save");
  checks.push(["notification switch saves one store", Object.keys(single.body.changes).length === 1 && single.body.changes["102"].monitored === false]);
  checks.push(["toast shown", $("toasts").children.length > 0]);

  click(doc.querySelector('[data-view="notifications"]'));
  await tick();
  checks.push(["recipients", $("lead-company").textContent === "老板" && $("lead-ad").textContent === "未设置"]);
  const dailyPrompt = doc.querySelector('#lanes-form [data-lane="daily"] textarea[name="prompt"]');
  checks.push(["default task text shown", dailyPrompt.value === "日报默认说明"
    && doc.querySelector('[data-prompt-state="daily"]').textContent === "使用默认说明"]);
  checks.push(["no unsaved changes yet", $("lanes-savebar").hidden]);
  const weekly = doc.querySelector('#lanes-form [data-lane="review"] input[name="weekly"]');
  weekly.checked = true; change(weekly);
  checks.push(["unsaved changes appear", !$("lanes-savebar").hidden]);
  click($("lanes-revert"));
  checks.push(["revert restores the saved form", $("lanes-savebar").hidden && !weekly.checked]);
  const reviewPrompt = doc.querySelector('#lanes-form [data-lane="review"] textarea[name="prompt"]');
  reviewPrompt.value = "多看 ACOS 恶化"; input(reviewPrompt);
  checks.push(["custom prompt state", doc.querySelector('[data-prompt-state="review"]').textContent === "已自定义"]);
  $("lanes-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await tick();
  const saved = last("POST /console/api/reports");
  checks.push(["lanes saved with prompts", saved && saved.body.lanes.daily.prompt === "" && saved.body.lanes.review.prompt === "多看 ACOS 恶化"
    && saved.body.lanes.hourly.min_clicks === 15 && saved.body.lanes.review.mention_operators === true && !("ai" in saved.body.lanes.daily)]);
  checks.push(["saved form is clean", $("lanes-savebar").hidden]);
  checks.push(["mention switches", doc.querySelectorAll('#lanes-form input[name="mention_operators"]').length === 2]);
  click(doc.querySelector('[data-preview="daily"]'));
  await tick(120);
  const started = last("POST /console/api/reports/preview");
  checks.push(["preview runs in the background with the draft", started && started.body.lane === "daily" && started.body.draft.prompt === ""]);
  checks.push(["preview drawer rendered", !$("preview-drawer").hidden && $("preview-progress").hidden
    && $("preview-cards").querySelector("table") !== null && $("preview-cards").querySelector("strong").textContent === "全公司核心指标"
    && $("preview-info").textContent.includes("SparkX 查询 5 次")]);
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
  checks.push(["escape closes the drawer", $("preview-drawer").hidden]);
  click(doc.querySelector('[data-lead="ad"]'));
  await tick();
  checks.push(["lead dialog", !$("member-dialog").hidden && $("member-list").querySelectorAll('input[type="radio"]').length === 2
    && !$("member-clear").hidden]);
  click(doc.querySelector('#member-dialog [data-close]'));
  click($("load-groups"));
  await tick();
  checks.push(["group change", !$("group-change").hidden && $("group-select").options.length === 1]);

  click(doc.querySelector('[data-view="sparkx"]'));
  await tick();
  checks.push(["sparkx facts", $("sparkx-facts").textContent.includes("已保存（管理台）") && $("sparkx-badge").textContent === "已配置"]);
  checks.push(["skills listed", $("skills-rows").textContent.includes("sparkx-query-ads-performance") && $("skills-facts").textContent.includes("v1.3.7")
    && $("tool-chips").children.length === 2]);
  click($("sparkx-test"));
  await tick();
  checks.push(["sparkx test", !$("sparkx-profiles-panel").hidden && $("sparkx-status").textContent.includes("授权 1 家")]);

  click(doc.querySelector('[data-view="deepseek"]'));
  await tick();
  checks.push(["deepseek form", $("deepseek-model").options.length === 2 && $("deepseek-thinking").checked && $("deepseek-timeout").value === "120"]);
  checks.push(["bot badge", $("bot-badge").textContent === "已开启"]);
  $("deepseek-thinking").checked = false;
  $("deepseek-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await tick();
  checks.push(["deepseek save sends thinking", last("POST /console/api/deepseek").body.thinking === false]);
  click($("deepseek-test"));
  await tick();
  checks.push(["deepseek test", $("deepseek-status").textContent === "尚未配置密钥。"]);

  click(doc.querySelector('[data-view="deliveries"]'));
  await tick();
  checks.push(["deliveries", $("delivery-rows").textContent.includes("可能已提交（不自动重发）")
    && !doc.querySelector('#page-actions [data-for="deliveries"]').hidden]);
  const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
  console.log(JSON.stringify({ passed: checks.length - failed.length, failed, errors }, null, 1));
  process.exit(failed.length || errors.length ? 1 : 0);
})();
