const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() { this.children = []; this.dataset = {}; this.hidden = false; this.textContent = ""; }
  addEventListener() {}
  setAttribute() {}
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
