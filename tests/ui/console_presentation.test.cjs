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
  assert.equal(scope.elements.get("business-daily-note").textContent, "未取得：毛利润");
  assert.equal(scope.elements.get("metric-summary").hidden, true);
});
