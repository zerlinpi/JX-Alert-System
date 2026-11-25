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
    { tag: "table", columns: [{ name: "c0", display_name: "店铺" }, { name: "c1", display_name: "花费" }, { name: "c2", display_name: "运营" }],
      rows: [{ c0: "Campx", c1: "$4,034", c2: "高爽" }] }, { tag: "hr" }] } };
const defaults = { daily: "日报默认说明", review: "复盘默认说明", hourly: "" };
const lanes = { daily: { enabled: true, to_group: true, mention_operators: true, to_lead: true, prompt: "" },
  review: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, weekly: false, prompt: "" },
  hourly: { enabled: true, to_group: true, mention_operators: true, to_lead: true, to_operators: false, start_hour: 9, end_hour: 24,
            high_spend: 30, low_spend: 10, min_clicks: 15, acos_high: 60, cvr_low: 5, acos_good: 20, min_orders_good: 2, prompt: "" } };
const storeRows = () => [
  { profile_id: "101", name: "香港川页", short_name: "香港川页", country_label: "美国", store_type: "seller", authorized: true,
    monitored: true, operators: [{ id: "ou_op1", name: "皮泽霖" }] },
  { profile_id: "102", name: "Campx-主账号", short_name: "Campx", country_label: "美国", store_type: "seller", authorized: true,
    monitored: true, operators: [] },
  { profile_id: "105", name: "昌泉", short_name: "昌泉", country_label: "美国", store_type: "seller", authorized: true,
    monitored: false, operators: [] },
  { profile_id: "201", name: "川页-CA", short_name: "川页-CA", country_label: "加拿大", store_type: "seller", authorized: true,
    monitored: true, operators: [{ id: "ou_op1", name: "皮泽霖" }] }];
// Preview jobs as the server keeps them: a lane's job is reused until 「重新生成」 (fresh) asks for a new one.
const jobs = {};
let nextJob = 0;
const startedAt = new Date(Date.now() - 65000).toISOString();
const doneResult = { cards: [card], text: "SparkX 广告日报",
  info: { profiles: 2, report_date: "2026-11-22", sparkx_calls: 5, seconds: 70, usage: { total_tokens: 183000, prompt_cache_hit_tokens: 102000 },
          mentions: [{ name: "高爽", stores: ["Campx"] }], excel: true } };
const view = (id) => ({ job: id, ...jobs[id].state });
// The Skills page as the server answers it: the official catalog with each Skill's state.
const skillsView = (disabled) => ({ installed: { release: "v1.3.7", commit: "b72b914b8", installed_at: "2026-11-24T02:00:00Z" },
  skills: [{ name: "sparkx-query-ads-performance", version: "1.4.11", description: "Query metrics.", mcp_tools: ["get_ads_perf"], required: true }],
  catalog: [
    { name: "sparkx-query-ads-performance", version: "1.4.11", description: "Query metrics.", mcp_tools: ["get_ads_perf"], required: true,
      state: "on", tools: [] },
    { name: "sparkx-weekly-ads-report", version: "1.1.6", description: "Weekly.", mcp_tools: ["get_ads_perf"], required: false,
      state: disabled.includes("sparkx-weekly-ads-report") ? "off" : "on", tools: [] },
    { name: "sparkx-query-market-data", version: "1.0.1", description: "Market.", mcp_tools: ["get_market_search_perf"], required: false,
      state: "missing_tools", tools: ["get_market_search_perf"] },
    { name: "sparkx-edit-ads", version: "1.3.0", description: "Edit.", mcp_tools: ["batch_update_ads"], required: true, write: true,
      state: writeOn ? "on" : "write_off", tools: writeOn ? [] : ["batch_update_ads"] },
    { name: "sparkx-future-tool", version: "0.1.0", description: "Needs a tool JX never offers.", mcp_tools: ["list_skills"], required: false,
      write: false, state: "unsupported", tools: ["list_skills"] }],
  choices: { revision: disabled.length ? "7".repeat(64) : "0".repeat(64), disabled },
  read_tools: ["get_ads_perf", "get_entity_metadata"], write_tools: ["batch_update_ads"], source: "SparkXAI/SparkXAI-MCP",
  latest: null, error: null });
// The 「广告写入」 switch and its audit log as the server answers them.
let writeOn = false;
const writesView = () => ({ revision: (writeOn ? "8" : "0").repeat(64), enabled: writeOn, updated_at: null,
  enabled_at: writeOn ? "2026-11-26T01:00:00Z" : null, error: null, leads: ["老板", "广告主管"], confirm_word: "确认执行",
  write_tools: ["batch_update_ads", "create_portfolio"], offered_write_tools: ["batch_update_ads"], max_writes: 30,
  log: [{ id: 7, at: "2026-11-26T01:05:00Z", status: "EXECUTED", status_label: "已执行", tool: "batch_update_ads", requester: "老板",
    request: '{"profileId":101,"request":{"confirmToken":"***"}}', result: '{"status":"SUCCESS"}' },
  { id: 6, at: "2026-11-26T01:04:00Z", status: "BLOCKED", status_label: "已拦截，未执行", tool: "batch_update_ads", requester: "老板",
    request: '{"profileId":101}', result: "" }] });
const responses = {
  "GET /console/api/overview": () => ({ sparkx: { configured: true, token_source: "console" }, deepseek: { configured: true, model: "deepseek-flash" },
    skills: { installed: false, release: null, count: 0 },
    feishu: { delivery_enabled: true, scheduler_enabled: true, group: "日报群", company_lead: false, ad_lead: true, bot_enabled: true },
    stores: { authorized: 4, monitored: 3, mapped: 2, roster_at: "2026-11-24T00:30:00Z", sites: { "美国": 2, "加拿大": 1 } },
    lanes: { daily: { enabled: true, last: { status: "SUCCEEDED", at: "2026-11-24T00:31:00Z", queued: 2, sparkx_calls: 6, tokens: 183000, model: "deepseek-flash" } },
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
  "GET /console/api/recipients": () => ({ revision: "1".repeat(64), group: "日报群",
    company: [{ id: "ou_lead", name: "老板" }, { id: "ou_op2", name: "高爽" }], ad: [], max: 10, group_detail: { name: "日报群" } }),
  "POST /console/api/recipients/company": (body) => ({ revision: "5".repeat(64), group: "日报群", max: 10, ad: [],
    company: body.open_ids.map((id) => ({ id, name: id })) }),
  "GET /console/api/archive": () => ({ rows: [{ path: "daily/2026-11/广告日报_2026-11-24.xlsx", name: "广告日报_2026-11-24.xlsx",
    lane: "daily", lane_label: "08:30 日报", month: "2026-11", size: 12595, saved_at: "2026-11-25T00:31:00Z" }] }),
  "POST /console/api/skills/choices": (body) => skillsView(body.disabled),
  "GET /console/api/reports": () => ({ revision: "2".repeat(64), updated_at: null, lanes, default_prompts: defaults, max_prompt: 4000 }),
  "POST /console/api/reports": (body) => ({ revision: "3".repeat(64), updated_at: "2026-11-24T01:00:00Z", lanes: body.lanes, default_prompts: defaults }),
  "POST /console/api/reports/preview": (body) => {
    const own = Object.keys(jobs).filter((id) => jobs[id].lane === body.lane);
    const current = own.length ? own[own.length - 1] : null;
    if (current && (jobs[current].state.status === "running" || !body.fresh)) return { ...view(current), reused: true, current: true };
    const id = "job-" + (++nextJob);
    jobs[id] = { lane: body.lane, polls: 0, state: { lane: body.lane, status: "running", started_at: startedAt, finished_at: null, result: null, error: null, sent: [] } };
    return { ...view(id), reused: false, current: true };
  },
  "POST /console/api/reports/test-send": (body) => {
    const sent = [...(body.to_group ? ["group"] : []), ...(body.to_lead ? ["lead"] : [])];
    jobs[body.job].state.sent = sent;
    return { status: "QUEUED", queued: 2, cards: 1, files: 1, mentions: body.mention ? 1 : 0, targets: sent, sent };
  },
  "GET /console/api/groups": () => ({ chat_id: "oc_reports", name: "日报群", revision: "1".repeat(64), chats: [{ id: "oc_reports", name: "日报群" }] }),
  "GET /console/api/sparkx": () => ({ revision: "0".repeat(64), configured: true, token_source: "console", endpoint: "https://mcp.sparkx.cn/mcp" }),
  "GET /console/api/skills": () => skillsView([]),
  "GET /console/api/sparkx/write": () => writesView(),
  "POST /console/api/sparkx/write": (body) => { writeOn = body.enabled; return writesView(); },
  "POST /console/api/sparkx/test": () => ({ status: "OK", profile_count: 1, profiles: [{ profile_id: "101", name: "香港川页", country: "US", store_type: "seller" }] }),
  "GET /console/api/deepseek": () => ({ revision: "0".repeat(64), configured: true, models: ["deepseek-flash", "deepseek-v4-pro"],
    model: "deepseek-flash", timeout: 120, thinking: true, key_source: "console" }),
  "POST /console/api/deepseek": (body) => ({ revision: "4".repeat(64), configured: true, models: ["deepseek-flash", "deepseek-v4-pro"],
    model: body.model, timeout: body.timeout, thinking: body.thinking }),
  "POST /console/api/deepseek/test": () => ({ status: "NOT_CONFIGURED" }),
  "GET /console/api/deliveries": () => ({ rows: [{ created_at: "2026-11-24T00:31:00Z", lane: "08:30 日报（测试）", recipient: "通知群",
    title: "广告日报 · 2026-11-22", status: "DELIVERY_UNKNOWN", error: "可能已提交飞书", delivered_at: null }] }),
};
let hold = true;  // DeepSeek keeps "working" until the test releases it
const prefixes = {
  "GET /console/api/reports/preview?job=": (body, key) => {
    const id = decodeURIComponent(key.split("job=")[1]);
    const job = jobs[id];
    job.polls += 1;
    if (!hold && job.state.status === "running") {
      Object.assign(job.state, { status: "done", finished_at: new Date().toISOString(), result: JSON.parse(JSON.stringify(doneResult)) });
    }
    return view(id);
  },
  "GET /console/api/reports/preview?lane=": () => ({ job: null, status: null }),
};
window.fetch = async (url, init = {}) => {
  const key = `${init.method || "GET"} ${url}`;
  const body = init.body ? JSON.parse(init.body) : undefined;
  requests.push({ key, headers: init.headers || {}, body });
  const prefix = Object.keys(prefixes).find((value) => key.startsWith(value));
  const handler = responses[key] || (prefix && prefixes[prefix]);
  if (!handler) return { status: 404, ok: false, json: async () => ({ error: "未模拟 " + key }) };
  return { status: 200, ok: true, json: async () => handler(body, key) };
};
window.eval(script);
const tick = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (id) => window.document.getElementById(id);
const doc = window.document;
const click = (node) => node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
const change = (node) => node.dispatchEvent(new window.Event("change", { bubbles: true }));
const input = (node) => node.dispatchEvent(new window.Event("input", { bubbles: true }));
const last = (key) => [...requests].reverse().find((request) => request.key === key);
const count = (key) => requests.filter((request) => request.key === key).length;
const locked = () => doc.documentElement.classList.contains("scroll-locked");
(async () => {
  window.JXConsole.start(doc, window);
  await tick();
  const checks = [];
  checks.push(["overview title and actions", $("page-title").textContent === "概览"
    && !doc.querySelector('#page-actions [data-for="overview"]').hidden && doc.querySelector('#page-actions [data-for="stores"]').hidden]);
  checks.push(["setup checklist shows the missing steps", !$("setup-panel").hidden && $("setup-steps").children.length === 6
    && $("setup-progress").textContent === "已完成 3/5"]);
  checks.push(["overview stats", $("status-cards").children.length === 4 && $("status-cards").textContent.includes("deepseek-flash")
    && $("status-cards").textContent.includes("纳入通知：美国 2 · 加拿大 1")]);
  checks.push(["lane rows", $("lane-rows").children.length === 3 && $("lane-rows").textContent.includes("查询 SparkX 6 次")
    && $("lane-rows").textContent.includes("Token 18.3 万（deepseek-flash）")]);
  checks.push(["recent rows", $("recent-rows").textContent.includes("已送达")]);

  click(doc.querySelector('[data-view="stores"]'));
  await tick();
  checks.push(["stores page", !doc.querySelector('[data-panel="stores"]').hidden && $("page-title").textContent === "店铺与运营"
    && $("store-rows").children.length === 4 && $("store-rows").textContent.includes("未映射运营")]);
  checks.push(["store filter counts", doc.querySelector('[data-count="unmapped"]').textContent === "1"
    && doc.querySelector('[data-count="paused"]').textContent === "1"]);
  checks.push(["site options", [...$("store-site").options].map((option) => option.textContent).join("|") === "全部站点（4）|美国（3）|加拿大（1）"]);
  $("store-site").value = "加拿大"; change($("store-site"));
  await tick();
  checks.push(["site filter", $("store-rows").children.length === 1 && $("store-rows").textContent.includes("川页-CA")
    && doc.querySelector('[data-count="all"]').textContent === "1"]);
  $("store-site").value = ""; change($("store-site"));
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
    && $("member-list").querySelectorAll('input[type="checkbox"]').length === 2 && locked()]);
  const op2 = $("member-list").querySelectorAll("input")[1];
  op2.checked = true; change(op2);
  click($("member-save"));
  await tick();
  const bulk = last("POST /console/api/stores/save");
  checks.push(["bulk save sends csrf and both stores", bulk && bulk.headers["X-CSRF-Token"] === "a".repeat(64)
    && JSON.stringify(Object.keys(bulk.body.changes).sort()) === JSON.stringify(["101", "102"])
    && bulk.body.changes["102"].operator_ids[0] === "ou_op2" && bulk.body.changes["101"].monitored === true]);
  checks.push(["dialog closed, page unlocked and selection cleared", $("member-dialog").hidden && !locked() && $("bulk-bar").hidden
    && $("store-rows").textContent.includes("高爽")]);
  const monitor = $("store-rows").querySelectorAll('input[role="switch"]')[1];
  monitor.checked = false; change(monitor);
  await tick();
  const single = last("POST /console/api/stores/save");
  checks.push(["notification switch saves one store", Object.keys(single.body.changes).length === 1 && single.body.changes["102"].monitored === false]);
  checks.push(["toast shown", $("toasts").children.length > 0]);

  click(doc.querySelector('[data-view="notifications"]'));
  await tick();
  checks.push(["recipients list several leads", $("lead-company").textContent === "老板、高爽" && $("lead-ad").textContent === "未设置"]);
  checks.push(["earlier previews are looked up per lane", count("GET /console/api/reports/preview?lane=daily") === 1
    && count("GET /console/api/reports/preview?lane=hourly") === 1]);
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
    && saved.body.lanes.hourly.min_clicks === 15 && saved.body.lanes.review.mention_operators === true
    && saved.body.lanes.daily.mention_operators === true && !("ai" in saved.body.lanes.daily)]);
  checks.push(["saved form is clean", $("lanes-savebar").hidden]);
  checks.push(["mention switches on every lane", doc.querySelectorAll('#lanes-form input[name="mention_operators"]').length === 3]);

  // Preview: runs in the background, survives closing the drawer, and is not generated twice.
  click(doc.querySelector('[data-preview="daily"]'));
  await tick();
  const started = last("POST /console/api/reports/preview");
  checks.push(["preview runs in the background with the draft", started && started.body.lane === "daily" && started.body.draft.prompt === ""
    && !started.body.fresh && !$("preview-drawer").hidden && locked()]);
  checks.push(["progress counts from the server start time", !$("preview-progress").hidden
    && /已用 1 分 \d+ 秒/.test($("preview-progress-text").textContent) && $("test-send").disabled]);
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
  checks.push(["closing while running keeps the job", $("preview-drawer").hidden && !locked()
    && doc.querySelector('[data-preview-state="daily"]').textContent.startsWith("预览生成中")]);
  hold = false;
  await tick(120);
  checks.push(["the lane shows the finished preview", doc.querySelector('[data-preview-state="daily"]').textContent.startsWith("已生成预览")
    && jobs["job-1"].polls >= 2]);
  click(doc.querySelector('[data-preview="daily"]'));
  await tick();
  checks.push(["reopening shows it without a new run", Object.keys(jobs).length === 1 && !$("preview-drawer").hidden
    && $("preview-progress").hidden && $("preview-cards").querySelector("table") !== null
    && $("preview-cards").querySelector("strong").textContent === "全公司核心指标"
    && $("preview-notice").textContent.includes("没有重新调用 DeepSeek")
    && $("preview-info").textContent.includes("SparkX 查询 5 次") && $("preview-info").textContent.includes("Token 18.3 万（缓存命中 10.2 万）")]);
  checks.push(["test send is offered for the finished preview", !$("test-send").disabled && $("test-lead-label").textContent === "总负责人（老板、高爽）"
    && !$("test-to-lead").disabled && $("test-mention-label").textContent === "@ 对应运营：高爽（Campx）" && !$("test-mention").disabled
    && $("test-send-hint").textContent.includes("和它的 Excel 文件")]);
  click($("test-send-button"));
  await tick();
  const test = last("POST /console/api/reports/test-send");
  checks.push(["test send posts only the job and the chosen recipients", test && test.body.job === "job-1" && test.body.to_group === true
    && test.body.to_lead === false && test.body.mention === false && Object.keys(test.body).length === 4
    && $("test-send-hint").textContent.startsWith("已发送测试到：通知群")
    && [...$("toasts").children].some((node) => node.textContent.includes("1 张卡片 + 1 个 Excel"))]);
  hold = true;
  click($("preview-regenerate"));
  await tick();
  const fresh = last("POST /console/api/reports/preview");
  checks.push(["regenerate asks for a fresh run", fresh.body.fresh === true && Object.keys(jobs).length === 2 && $("test-send").disabled
    && !$("preview-progress").hidden && $("preview-regenerate").disabled]);
  hold = false;
  await tick(120);
  checks.push(["the new preview replaces the old one", $("preview-progress").hidden && !$("test-send").disabled
    && $("test-send-hint").textContent.startsWith("发送的就是上面这份预览")]);
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
  click(doc.querySelector('[data-test="review"]'));
  await tick(120);
  checks.push(["「测试发送」 opens the preview and focuses sending", !$("preview-drawer").hidden && $("preview-title").textContent === "预览 · 广告异常复盘"
    && doc.activeElement === $("test-send-button") && $("test-lead-label").textContent === "广告负责人（未设置）" && $("test-to-lead").disabled]);
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
  checks.push(["escape closes the drawer", $("preview-drawer").hidden && !locked()]);
  click(doc.querySelector('[data-lead="company"]'));
  await tick();
  const leadBoxes = $("member-list").querySelectorAll('input[type="checkbox"]');
  checks.push(["lead dialog allows several people", !$("member-dialog").hidden && leadBoxes.length === 2 && !leadBoxes[0].checked
    && leadBoxes[1].checked && $("member-hint").textContent.includes("最多 10 位") && !$("member-clear").hidden
    && $("member-list").querySelectorAll('input[type="radio"]').length === 0]);
  leadBoxes[0].checked = true; change(leadBoxes[0]);
  click($("member-save"));
  await tick();
  const leadSave = last("POST /console/api/recipients/company");
  checks.push(["several leads are saved together", leadSave && $("member-dialog").hidden
    && JSON.stringify([...leadSave.body.open_ids].sort()) === JSON.stringify(["ou_lead", "ou_op1", "ou_op2"])]);
  click($("load-groups"));
  await tick();
  checks.push(["group change", !$("group-change").hidden && $("group-select").options.length === 1]);

  click(doc.querySelector('[data-view="sparkx"]'));
  await tick();
  checks.push(["sparkx facts", $("sparkx-facts").textContent.includes("已保存（管理台）") && $("sparkx-badge").textContent === "已配置"]);
  checks.push(["skills listed", $("skills-rows").textContent.includes("sparkx-query-ads-performance") && $("skills-facts").textContent.includes("v1.3.7")
    && $("tool-chips").children.length === 2]);
  const skillBox = (name) => $("skills-rows").querySelector(`input[data-skill="${name}"]`);
  checks.push(["every official skill shows its state", $("skills-rows").children.length === 5
    && skillBox("sparkx-query-ads-performance").checked && skillBox("sparkx-query-ads-performance").disabled
    && skillBox("sparkx-weekly-ads-report").checked && !skillBox("sparkx-weekly-ads-report").disabled
    && !skillBox("sparkx-edit-ads").checked && skillBox("sparkx-edit-ads").disabled
    && !skillBox("sparkx-future-tool").checked && skillBox("sparkx-future-tool").disabled
    && $("skills-rows").textContent.includes("写入未开启") && $("skills-rows").textContent.includes("开启「广告写入」后启用")
    && $("skills-rows").textContent.includes("写入（随「广告写入」开关）")
    && $("skills-rows").textContent.includes("需要本系统不提供的工具：list_skills")
    && $("skills-rows").textContent.includes("当前 SparkX Token 没有提供")
    && $("skills-summary").textContent === "已安装 4 个（写入 1 个），使用中 2 个；1 个需要本系统不提供的工具，未安装"]);
  checks.push(["the write switch starts off and lists who may write", $("writes-badge").textContent === "已关闭（只读）"
    && !$("writes-enabled").checked && $("writes-confirm-row").hidden && $("writes-facts").textContent.includes("老板、广告主管")
    && $("writes-facts").textContent.includes("确认执行") && $("writes-facts").textContent.includes("batch_update_ads")
    && $("writes-rows").children.length === 2 && $("writes-rows").textContent.includes("已拦截，未执行")
    && $("writes-rows").textContent.includes('"confirmToken":"***"')]);
  $("writes-enabled").checked = true; change($("writes-enabled"));
  checks.push(["switching on asks for a confirmation first", !$("writes-confirm-row").hidden && !$("writes-confirm").checked]);
  const before = count("POST /console/api/sparkx/write");
  $("writes-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await tick();
  checks.push(["without the confirmation nothing is saved", count("POST /console/api/sparkx/write") === before]);
  $("writes-confirm").checked = true;
  $("writes-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await tick();
  const writeSave = last("POST /console/api/sparkx/write");
  checks.push(["the switch is saved with its revision and the confirmation", writeSave && writeSave.body.enabled === true
    && writeSave.body.confirmed === true && writeSave.body.revision === "0".repeat(64) && Object.keys(writeSave.body).length === 3
    && $("writes-badge").textContent === "已开启" && $("writes-confirm-row").hidden
    && skillBox("sparkx-edit-ads").checked && skillBox("sparkx-edit-ads").disabled]);
  $("writes-enabled").checked = false; change($("writes-enabled"));
  $("writes-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await tick();
  const writeOff = last("POST /console/api/sparkx/write");
  checks.push(["switching off needs no confirmation", writeOff.body.enabled === false && writeOff.body.confirmed === false
    && writeOff.body.revision === "8".repeat(64) && $("writes-badge").textContent === "已关闭（只读）"]);
  const weeklySkill = skillBox("sparkx-weekly-ads-report");
  weeklySkill.checked = false; change(weeklySkill);
  await tick();
  const choice = last("POST /console/api/skills/choices");
  checks.push(["switching a skill off saves the choice", choice && JSON.stringify(choice.body.disabled) === JSON.stringify(["sparkx-weekly-ads-report"])
    && choice.body.revision === "0".repeat(64) && !skillBox("sparkx-weekly-ads-report").checked
    && $("skills-rows").textContent.includes("已停用")]);
  click($("sparkx-test"));
  await tick();
  checks.push(["sparkx test", !$("sparkx-profiles-panel").hidden && $("sparkx-status").textContent.includes("授权 1 家")]);

  click(doc.querySelector('[data-view="deepseek"]'));
  await tick();
  checks.push(["deepseek form", $("deepseek-model").options.length === 2 && $("deepseek-model").value === "deepseek-flash"
    && $("deepseek-thinking").checked && $("deepseek-timeout").value === "120"]);
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
  checks.push(["deliveries", $("delivery-rows").textContent.includes("可能已提交（不自动重发）") && $("delivery-rows").textContent.includes("（测试）")
    && !doc.querySelector('#page-actions [data-for="deliveries"]').hidden]);
  const download = $("archive-rows").querySelector("a");
  checks.push(["excel archive lists downloadable files", $("archive-rows").textContent.includes("广告日报_2026-11-24.xlsx")
    && $("archive-rows").textContent.includes("12.3 KB") && $("archive-rows").textContent.includes("08:30 日报") && download
    && download.getAttribute("href") === "/console/api/archive/file?path=" + encodeURIComponent("daily/2026-11/广告日报_2026-11-24.xlsx")
    && download.getAttribute("download") === "广告日报_2026-11-24.xlsx"]);
  const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
  console.log(JSON.stringify({ passed: checks.length - failed.length, failed, errors }, null, 1));
  process.exit(failed.length || errors.length ? 1 : 0);
})();
