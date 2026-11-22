const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.hidden = false; this.textContent = "";
    this.attributes = new Map(); this.listeners = new Map();
  }
  addEventListener(name, handler) { this.listeners.set(name, handler); }
  dispatch(name, extra = {}) { return this.listeners.get(name)?.({preventDefault() {}, ...extra}); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  getClientRects() { return this.hidden ? [] : [{}]; }
  focus() { this.focused = true; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}

function consoleScope() {
  const elements = new Map();
  const context = vm.createContext({
    Intl, Number, Node: Element, Set, Map,
    fetch: () => new Promise(() => {}),
    document: {
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => new Element(),
      createDocumentFragment: () => new Element(),
      getElementById: (id) => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
      },
    },
  });
  vm.runInContext(readFileSync(resolve(__dirname, "../../src/jx_alert/admin_ui/console.js"), "utf8"), context);
  return { context, elements, evaluate: (expression) => vm.runInContext(expression, context) };
}

test("business report keeps real zeros, percent points and signed losses", () => {
  const scope = consoleScope();
  assert.equal(scope.evaluate('businessValue(0, "money", "USD")'), "$0.00");
  assert.equal(scope.evaluate('businessValue(-387.22, "money", "USD")'), "$-387.22");
  assert.equal(scope.evaluate('businessValue(0.1218, "rate")'), "12.18%");
  assert.equal(scope.evaluate('businessComparison({unit:"rate", change:-0.0525})'), "-5.25 个百分点");
  assert.equal(scope.evaluate('businessComparison({unit:"count", change:0.25})'), "+25.00%");
});

test("unknown money, different currency and unfinished dates cannot show a valid comparison", () => {
  const scope = consoleScope();
  assert.equal(scope.evaluate('businessValue(null, "money", "USD")'), "—");
  assert.equal(scope.evaluate('businessValue(100, "money", null)'), "币种待确认");
  assert.equal(scope.evaluate('businessComparison({unit:"money", change:0.2, comparable:false})'), "币种不同");
  assert.equal(scope.evaluate('businessComparison({unit:"money", change:0.2}, "数据尚未完整")'), "待确认");
  assert.equal(scope.evaluate('businessComparison({unit:"money", current:50, previous:0, change:null})'), "前值为零");
});

test("no dashboard data gives one useful message and preserves the distinct sales summary", () => {
  const scope = consoleScope();
  scope.evaluate('renderBusinessDaily({business_source_configured:false, business_daily:{metrics:[]}})');
  assert.equal(scope.elements.get("business-daily-note").textContent, "经营看板数据尚未接入。");
  assert.equal(scope.elements.get("business-daily-table").hidden, true);
  assert.equal(scope.elements.get("metric-summary").hidden, false);
  scope.evaluate('renderBusinessDaily({})');
  assert.equal(scope.elements.get("business-daily").hidden, true);
});

test("compact business rows display source evidence and omit entirely unavailable metrics", () => {
  const scope = consoleScope();
  scope.evaluate(`renderBusinessDaily({business_source_configured:true, business_daily:{
    report_date:"2026-11-16", previous_date:"2026-11-15", currency:"USD", missing_metrics:["毛利润"], metrics:[
      {label:"销量", current:0, previous:20, unit:"count", change:-1},
      {label:"净销售额", current:4089.75, previous:3524.80, unit:"money", currency:"USD", previous_currency:"USD", change:0.1603},
      {label:"毛利润", current:null, previous:null, unit:"money"}
    ]}})`);
  const rows = scope.elements.get("business-daily-rows").children[0].children;
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].children.map((cell) => cell.textContent), ["销量", "0", "20", "-100.00%"]);
  assert.deepEqual(rows[1].children.map((cell) => cell.textContent), ["净销售额", "$4,089.75", "$3,524.80", "+16.03%"]);
  assert.equal(scope.elements.get("business-daily-note").textContent, "");
  assert.equal(scope.elements.get("metric-summary").hidden, true);
});

test("source replacement dates use calendar months and raw naive time is not guessed", () => {
  const scope = consoleScope();
  assert.equal(scope.evaluate('nextSourceMonth("2026-01-31")'), "2026-02-28");
  assert.equal(scope.evaluate('nextSourceMonth("2028-01-31")'), "2028-02-29");
  assert.equal(scope.evaluate('nextSourceMonth("2026-12-17")'), "2027-01-17");
  assert.equal(scope.evaluate('sourceTime("2026-11-17 12:00:00")'), "");
  assert.equal(scope.evaluate('sourceTime(null)'), "");
});

test("source screen shows metadata and real audit entries without secret placeholders", () => {
  const scope = consoleScope();
  scope.evaluate(`state.csrf="${"a".repeat(64)}"; renderSources({revision:"${"b".repeat(64)}",
    api:{configured:true,status:"CONFIGURED",origin:"https://openapi.lingxing.com"},
    mcp:{configured:false,status:"NOT_CONFIGURED",origin:"https://openmcp.lingxing.com"},
    events:[{kind:"mcp",action:"ADD",at:"2026-11-17T08:00:00+00:00",expires_on:"2026-12-17"}]});`);
  assert.equal(scope.elements.get("source-api-fields").disabled, false);
  assert.equal(scope.elements.get("source-mcp-state").children[0].textContent, "未配置");
  const rows = scope.elements.get("source-history-rows").children[0].children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].children[1].textContent, "添加");
  assert.equal(rows[0].children[3].textContent, "2026-12-17");
  assert.equal(scope.elements.has("source-mcp-key"), false);
});

test("saving a source uses same-origin CSRF and clears secret fields", async () => {
  const scope = consoleScope();
  const metadata = {revision:"b".repeat(64),api:{configured:true,status:"CONFIGURED"},mcp:{configured:true,status:"CONFIGURED"},events:[]};
  let request;
  scope.context.fetch = async (url, options) => {
    request = {url, ...options};
    return {status:200, ok:true, redirected:false, json:async () => metadata};
  };
  scope.evaluate(`state.csrf="${"a".repeat(64)}"; state.sources.data=${JSON.stringify(metadata)};`);
  for (const [id,value] of Object.entries({"source-mcp-url":"https://openmcp.lingxing.com/mcp-servers/lingxing-mcp", "source-mcp-key":"private-test-key-never-returned", "source-mcp-expiry":"2026-12-17"})) {
    scope.context.document.getElementById(id).value=value;
  }
  await scope.evaluate('saveSource("mcp")');
  assert.equal(request.url, "/console/api/data-sources");
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.headers["X-CSRF-Token"], "a".repeat(64));
  const body = JSON.parse(request.body);
  assert.equal(body.kind, "mcp");
  assert.equal(body.credentials.key, "private-test-key-never-returned");
  assert.equal(body.expires_on, "2026-12-17");
  assert.equal(scope.elements.get("source-mcp-key").value, "");
  assert.equal(scope.evaluate('JSON.stringify(state.sources.data).includes("private-test-key-never-returned")'), false);
});

test("unknown-only rows are omitted instead of shown as uncertain values", () => {
  const scope = consoleScope();
  scope.evaluate(`renderBusinessDaily({business_source_configured:true,business_daily:{availability:"AVAILABLE",metrics:[
    {label:"旧值",current:null,previous:5,unit:"count"},
    {label:"币种缺失",current:500,previous:300,unit:"money",currency:null},
    {label:"销量",current:0,previous:2,unit:"count",change:-1}]}})`);
  const rows = scope.elements.get("business-daily-rows").children[0].children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].children[0].textContent, "销量");
  assert.equal(rows[0].children[1].textContent, "0");
});

test("MCP fallback has its own basis and never duplicates valid primary facts", () => {
  const scope = consoleScope();
  scope.evaluate(`renderSourceObservations({metrics:{orders:3},ad_breakdown:[{type:"SP",spend:80,clicks:10,impressions:500}],source_observations:{
    report_date:"2026-11-17",store_sales:{basis:"下单口径",currency:"USD",metrics:{sales:400,units:4,orders:4}},
    campaign_ads:[{ad_type:"SP",currency:"USD",basis:"单日观测",metrics:{spend:99,clicks:12,impressions:600}}]}})`);
  const rows = scope.elements.get("source-observation-rows").children[0].children;
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.children[0].textContent), ["销售额（下单口径）", "销量（下单口径）"]);
  assert.equal(scope.elements.get("source-observations-basis").textContent, "2026-11-17 · 下单口径");
});

test("MCP unknown currency does not expose a guessed spend amount", () => {
  const scope = consoleScope();
  scope.evaluate(`renderSourceObservations({source_observations:{report_date:"2026-11-17",store_sales:null,
    campaign_ads:[{ad_type:"SP",currency:null,basis:"单日观测",metrics:{spend:999,clicks:0,impressions:50,orders:8}}]}})`);
  const rows = scope.elements.get("source-observation-rows").children[0].children;
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children[0].textContent, "SP · 点击");
  assert.equal(rows[0].children[1].textContent, "0");
  assert.equal(rows[1].children[1].textContent, "50");
});

test("API business observations show known values with their independent date bases", () => {
  const scope = consoleScope();
  scope.evaluate(`renderSourceObservations({business_daily:{availability:"MISSING"},api_business_observations:{report_date:"2026-11-16",
    product:{basis:"产品表现",currency:"USD",metrics:{volume:0,netAmount:100,adSpend:20,returnGoodsCount:5,refundCount:0},
      metric_bases:{volume:"下单日",netAmount:"下单日",returnGoodsCount:"退货发生日",refundCount:"退款发生日"}},
    settlement:{basis:"利润报表，结算日，已发放",currency:"USD",metrics:{grossProfit:-20,grossRate:-0.25,refundsRate:0.123},
      metric_bases:{grossProfit:"已发放结算日",grossRate:"已发放结算日",refundsRate:"已发放结算日"}}}})`);
  const rows = scope.elements.get("source-observation-rows").children[0].children;
  assert.deepEqual(rows.map(row => row.children[0].textContent), ["销量（下单日）", "净销售额（下单日）",
    "退货量（退货发生日）", "退款量（退款发生日）", "毛利润（已发放结算日）", "毛利率（已发放结算日）",
    "退款率（已发放结算区间）", "广告费/净销售额（产品表现）"]);
  assert.equal(rows[0].children[1].textContent, "0");
  assert.equal(rows[2].children[1].textContent, "5");
  assert.equal(rows[3].children[1].textContent, "0");
  assert.equal(rows[5].children[1].textContent, "-25.00%");
  assert.equal(rows[6].children[1].textContent, "12.30%");
  assert.equal(rows[7].children[1].textContent, "20.00%");
  assert.equal(scope.elements.get("source-observations-basis").textContent, "2026-11-16 · 产品表现 · 利润报表，结算日，已发放");
});

test("legacy settlement refund rate keeps its source value without implying a denominator", () => {
  const scope = consoleScope();
  scope.evaluate(`renderSourceObservations({api_business_observations:{report_date:"2026-11-16",
    settlement:{basis:"利润报表，结算日，已发放",currency:"USD",metrics:{refundsRate:0}}}})`);
  const rows = scope.elements.get("source-observation-rows").children[0].children;
  assert.deepEqual(rows.map(row => row.children.map(cell => cell.textContent)), [["退款率（结算口径）", "0.00%"]]);
});

test("legacy product response cannot label event counts using its blanket order date", () => {
  const scope = consoleScope();
  scope.evaluate(`renderSourceObservations({api_business_observations:{report_date:"2026-11-16",
    product:{basis:"产品表现，下单日",currency:"USD",metrics:{volume:2,returnGoodsCount:5,refundCount:0}}}})`);
  const rows = scope.elements.get("source-observation-rows").children[0].children;
  assert.deepEqual(rows.map(row => row.children[0].textContent), ["销量"]);
  assert.equal(scope.elements.get("source-observations-basis").textContent, "2026-11-16 · 产品表现");
});

test("advertising private recipients require an explicit verified choice and can be cleared offline", () => {
  const scope = consoleScope();
  scope.evaluate('state.csrf="a".repeat(64); state.adSubscribers.data={revision:"b".repeat(64), recipients:[{id:"ou_first",name:"接收人"}]}; state.adSubscribers.selected=new Set(["ou_first"]); updateAdSubscribers()');
  assert.equal(scope.elements.get("save-ad-subscribers").disabled, true);
  scope.evaluate('state.adSubscribers.selected=new Set(["ou_unknown"]); updateAdSubscribers()');
  assert.equal(scope.elements.get("save-ad-subscribers").disabled, true);
  scope.evaluate('state.adSubscribers.selected.clear(); updateAdSubscribers()');
  assert.equal(scope.elements.get("save-ad-subscribers").disabled, false);
  assert.match(scope.elements.get("ad-subscriber-note").textContent, /不额外私聊/);
});

test("private recipient save uses CSRF and sends no report subscription flag", async () => {
  const scope = consoleScope();
  scope.evaluate('state.csrf="a".repeat(64); state.adSubscribers.data={revision:"b".repeat(64),recipients:[]}; state.adSubscribers.members=[{id:"ou_first",name:"接收人"}]; state.adSubscribers.selected=new Set(["ou_first"]); updateAdSubscribers()');
  let request;
  scope.context.fetch = async (url, options) => {
    request = {url,...options};
    return {ok:true,status:200,json:async()=>({revision:"c".repeat(64),recipients:[{id:"ou_first",name:"接收人"}]})};
  };
  await scope.evaluate('saveAdSubscribers()');
  assert.equal(request.url, '/console/api/ad-alert-subscribers');
  assert.equal(request.method, 'POST');
  assert.equal(request.headers['X-CSRF-Token'], 'a'.repeat(64));
  assert.deepEqual(JSON.parse(request.body), {open_ids:['ou_first'],revision:'b'.repeat(64)});
  assert.match(scope.elements.get('ad-subscriber-status').textContent, /不会补发旧告警/);
});

test("compact navigation only exposes links: Escape restores focus, repeated toggles do not reload", () => {
  const { context, elements } = consoleScope();
  let requests = 0;
  context.fetch = () => { requests++; return new Promise(() => {}); };
  const menu = elements.get("workspace-menu"), panel = elements.get("workspace-navigation");
  menu.dispatch("click");
  assert.equal(menu.getAttribute("aria-expanded"), "true");
  assert.equal(panel.dataset.expanded, "true");
  menu.dispatch("click");
  assert.equal(panel.dataset.expanded, "false");
  menu.dispatch("click");
  let prevented = false;
  panel.dispatch("keydown", { key: "Escape", preventDefault: () => { prevented = true; } });
  assert.ok(prevented && menu.focused);
  assert.equal(menu.getAttribute("aria-expanded"), "false");
  assert.equal(requests, 0);
});

test("each compact navigation choice opens the right view and moves focus to its title", async () => {
  const { context, elements, evaluate } = consoleScope();
  const views = ["analysis-settings", "notification-group", "data-sources", "owners", "overview"];
  const tabs = views.map((view) => Object.assign(new Element(), { dataset: {view} }));
  context.document.querySelectorAll = (selector) => selector === "[data-view]" ? tabs : [];
  let requests = 0;
  context.fetch = () => { requests++; return new Promise(() => {}); };
  evaluate("state.groups.current={}; state.adSubscribers.data={}; state.sources.data={}; state.deepseek.data={}; state.reviewSkills={}; state.skillLibrary={};");
  for (const [index, view] of views.entries()) {
    elements.get("workspace-menu").dispatch("click");
    await evaluate(`setView(${JSON.stringify(view)})`);
    assert.equal(elements.get("workspace-navigation").dataset.expanded, "false");
    assert.equal(elements.get("page-title").focused, true);
    assert.deepEqual(tabs.map((t) => t.getAttribute("aria-current")), tabs.map((_,i) => i === index ? "page" : null));
    assert.equal(elements.get("store-board").hidden, !["overview","owners"].includes(view));
    assert.equal(elements.get("fleet-strip").hidden, elements.get("store-board").hidden);
    assert.equal(elements.get("ownership").hidden, view !== "owners");
    for (const page of views.slice(0,3)) assert.equal(elements.get(page+"-panel").hidden, page !== view);
  }
  assert.equal(requests, 0);
});

test("compact store picker preserves search on dismissal and collapses on actual selection", () => {
  const { context, elements } = consoleScope();
  const button = elements.get("store-picker-toggle"), panel = elements.get("store-picker-panel");
  const search = elements.get("store-search");
  search.value = "北"; elements.get("store-filter").value = "all";
  const requests = [];
  context.clearTimeout = () => {};
  context.fetch = (url, options) => { requests.push({url, options}); return new Promise(() => {}); };
  button.dispatch("click");
  panel.dispatch("keydown", {key:"Escape"});
  assert.equal(panel.dataset.expanded, "false");
  assert.equal(search.value, "北");
  assert.equal(button.focused, true);
  assert.equal(requests.length, 0);
  button.dispatch("click");
  elements.get("stores").dispatch("click", { target:{ closest:() => ({dataset:{sid:"123"}}) } });
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.deepEqual(requests.map((r) => r.url), ["/console/api/stores/123"]);
  assert.equal(requests[0].options.method, undefined);
});
