"use strict";

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const moneyNumber = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });
const figures = [
  ["orders", "订单"], ["units", "销量"], ["sales", "销售额"],
  ["spend", "广告花费"], ["ad_orders", "广告归因订单"],
];
const initialCsrf = document.querySelector('meta[name="jx-console-csrf"]')?.content;
const state = {
  stores: [], sid: null, data: null, csrf: /^[0-9a-f]{64}$/.test(initialCsrf || "") ? initialCsrf : null,
  view: "overview", metric: "orders",
  storeSeq: 0, detailSeq: 0, memberSeq: 0, members: null, revision: null,
  selected: new Set(), saving: false, controlsDraft: null, controlsSaving: false, controlsLoading: false,
  controlsSeq: 0,
  collectionSeq: 0, collectionBusy: false, collectionTimer: null,
  bulkCollection: null, bulkBusy: false, bulkTimer: null,
  groups: { current: null, chats: [], preview: null, saving: false, loading: false, seq: 0 },
  sources: { data: null, loading: false, saving: false },
  deepseek: { data: null, loading: false, saving: false },
  reviewSkills: null,
  adSubscribers: { data: null, members: null, selected: new Set(), loading: false, saving: false },
};

function node(tag, text, name) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = String(text);
  if (name) el.className = name;
  return el;
}

function money(value, currency) {
  if (!Number.isFinite(value)) return "—";
  return currency ? `${moneyNumber.format(value)} ${currency}` : "币种待确认";
}

function scalar(value) {
  return Number.isFinite(value) ? number.format(value) : "—";
}

function display(value) {
  return value !== null && value !== undefined && value !== "" ? String(value) : "—";
}

function badge(value) {
  const text = display(value);
  const tone = /失败|异常|超标|被拒|恶化/.test(text) && !/未见|没有/.test(text) ? " bad"
    : /待|缺|不足|未授权|未成熟|未知/.test(text) ? " warn"
      : /采集完成|已授权|未见超标|健康$/.test(text) ? " good" : "";
  return node("span", text, "badge" + tone);
}

function notice(id, text = "", tone = "info") {
  const el = $(id);
  el.hidden = !text;
  el.dataset.tone = tone;
  el.textContent = text;
}

async function api(path, options) {
  const response = await fetch(`/console/api/${path}`, {
    ...options,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options?.headers || {}) },
  });
  if (response.status === 401 || response.redirected) {
    location.assign("/console/login");
    throw new Error("登录已过期，请重新登录。");
  }
  let json;
  try { json = await response.json(); } catch { throw new Error("服务暂时不可用，请刷新重试。"); }
  if (!response.ok) {
    const error = new Error(typeof json.error === "string" ? json.error : "请求失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return json;
}

function tableRows(id, rows, width, emptyText) {
  const body = $(id);
  const fragment = document.createDocumentFragment();
  for (const cells of rows) {
    const tr = node("tr");
    cells.forEach((value, i) => {
      const cell = node(i === 0 ? "th" : "td");
      if (i === 0) cell.scope = "row";
      if (value instanceof Node) cell.append(value);
      else cell.textContent = display(value);
      tr.append(cell);
    });
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = node("tr");
    const cell = node("td", emptyText, "muted");
    cell.colSpan = width;
    tr.append(cell);
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
}

function findVisibleStoreRows() {
  const query = $("store-search").value.trim().toLowerCase();
  const filter = $("store-filter").value;
  return state.stores.filter((s) => {
    const matches = [s.sid, s.name, s.country].some((v) => String(v ?? "").toLowerCase().includes(query));
    if (!matches) return false;
    if (filter === "unmapped") return s.owner_count === 0;
    if (filter === "mapped") return s.owner_count > 0;
    if (filter === "unauthorized") return s.ads_authorized !== true;
    if (filter === "collection_paused") return s.controls?.collection_enabled === false;
    if (filter === "notifications_paused") return s.controls?.notifications_enabled === false;
    return true;
  });
}

function renderStoreList() {
  const list = $("stores");
  const focusedSid = document.activeElement?.dataset.sid;
  const visible = findVisibleStoreRows();
  const fragment = document.createDocumentFragment();
  let nextFocus;
  for (const store of visible) {
    const sid = String(store.sid);
    const li = node("li");
    const btn = node("button", undefined, "store-option");
    btn.type = "button";
    btn.dataset.sid = sid;
    btn.setAttribute("aria-current", sid === state.sid ? "true" : "false");
    if (focusedSid === sid) nextFocus = btn;
    const meta = node("small");
    const ownerStatus = store.owner_count == null ? "映射待核实"
      : store.owner_count > 0 ? `已映射 ${store.owner_count} 人` : "待映射";
    meta.append(node("span", [sid, store.country].filter(Boolean).join(" · ")), badge(ownerStatus));
    btn.append(node("strong", store.name || sid), meta);
    const paused = [];
    if (store.controls?.collection_enabled === false) paused.push("采集已关闭");
    if (store.controls?.notifications_enabled === false) paused.push("通知已关闭");
    if (paused.length) btn.append(node("span", paused.join(" · "), "store-paused"));
    li.append(btn);
    fragment.append(li);
  }
  list.replaceChildren(fragment);
  if (nextFocus) nextFocus.focus({ preventScroll: true });
  $("list-count").textContent = `${visible.length} 家`;
  $("stores-empty").hidden = visible.length > 0;
  $("stores-empty").textContent = state.stores.length ? "没有符合条件的店铺。" : "暂无已配置的店铺。";
}

async function loadStores() {
  const seq = ++state.storeSeq;
  $("stores").setAttribute("aria-busy", "true");
  $("refresh").disabled = true;
  notice("global-status");
  try {
    const response = await api("stores");
    if (seq !== state.storeSeq) return;
    state.stores = response.stores || [];
    state.csrf = response.csrf;
    renderBulkCollection();
    for (const [field, id] of [["stores", "total-stores"], ["authorized", "total-authorized"], ["mapped", "total-mapped"]]) {
      $(id).textContent = scalar(response.totals?.[field]);
    }
    const selected = state.stores.find((s) => String(s.sid) === state.sid)
      ?? state.stores.find((s) => s.ads_authorized === true) ?? state.stores[0];
    if (selected) await selectStore(String(selected.sid));
    else { state.sid = null; detailPlaceholder("请先配置可访问的店铺。"); renderStoreList(); }
  } catch (error) {
    if (seq !== state.storeSeq) return;
    notice("global-status", error.message || "无法读取店铺列表。", "error");
    if (!state.data) detailPlaceholder("无法读取店铺数据，请点击刷新数据。");
  } finally {
    if (seq === state.storeSeq) {
      $("stores").setAttribute("aria-busy", "false");
      $("refresh").disabled = false;
    }
  }
}

function detailPlaceholder(text) {
  $("detail-content").hidden = true;
  $("detail-placeholder").hidden = false;
  $("detail-placeholder").textContent = text;
}

function renderBulkCollection() {
  const receipt = state.bulkCollection;
  $("sync-all").disabled = !state.csrf || state.bulkBusy || ["PENDING", "RUNNING"].includes(receipt?.status);
  $("bulk-collection-refresh").disabled = !state.csrf || state.bulkBusy;
  $("bulk-collection-details").hidden = !receipt?.stores?.length;
  if (!receipt || receipt.status === "NONE") { notice("bulk-collection-status"); return; }
  const outstanding = ["PENDING", "RUNNING"].includes(receipt.status) ? "待处理" : "未完成";
  const parts = [receipt.status_label, `本次 ${receipt.selected_store_count} 家`, `已完成 ${receipt.completed_store_count} 家`,
    `${outstanding} ${receipt.remaining_store_count} 家`];
  if (receipt.paused_store_count) parts.push(`途中关闭 ${receipt.paused_store_count} 家`);
  if (receipt.skipped_store_count) parts.push(`跳过 ${receipt.skipped_store_count} 家（${Object.entries(receipt.skipped_reason_counts || {}).map(([key, n]) => `${key} ${n}`).join("，")}）`);
  if (receipt.retry_at) {
    const retry = new Date(`${receipt.retry_at}Z`);
    if (!Number.isNaN(retry.getTime())) parts.push(`最早重试 ${retry.toLocaleTimeString("zh-CN")}`);
  }
  notice("bulk-collection-status", parts.join(" · "), /FAILED|BLOCKED/.test(receipt.status) ? "error" : "info");
  tableRows("bulk-collection-rows", (receipt.stores || []).map((r) => [r.store_name, r.report_date, badge(r.status_label)]), 3, "本次没有可同步店铺。");
}

async function bulkCollectionStatus({ start = false, polls = 0 } = {}) {
  if (!state.csrf || state.bulkBusy) return;
  state.bulkBusy = true;
  clearTimeout(state.bulkTimer);
  renderBulkCollection();
  let failed = false;
  try {
    const options = start ? { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: "{}" } : undefined;
    state.bulkCollection = await api("collection/all", options);
    renderBulkCollection();
    if (["PENDING", "RUNNING"].includes(state.bulkCollection.status) && polls < 60) {
      state.bulkTimer = setTimeout(() => void bulkCollectionStatus({ polls: polls + 1 }), 15000);
    }
  } catch (error) {
    failed = true;
    notice("bulk-collection-status", error.message || "暂时无法同步所有店铺，请稍后重试。", "error");
  } finally {
    state.bulkBusy = false;
    if (!failed) renderBulkCollection();
    else {
      $("sync-all").disabled = !state.csrf || ["PENDING", "RUNNING"].includes(state.bulkCollection?.status);
      $("bulk-collection-refresh").disabled = !state.csrf;
    }
  }
}

async function selectStore(sid, date) {
  const seq = ++state.detailSeq;
  ++state.memberSeq;
  ++state.controlsSeq;
  ++state.collectionSeq;
  clearTimeout(state.collectionTimer);
  state.collectionBusy = false;
  state.sid = sid;
  state.data = null;
  state.members = null;
  state.revision = null;
  state.selected = new Set();
  state.controlsDraft = null;
  state.controlsLoading = false;
  detailPlaceholder("正在读取店铺数据…");
  $("detail").setAttribute("aria-busy", "true");
  renderStoreList();
  const query = date ? `?date=${encodeURIComponent(date)}` : "";
  try {
    const data = await api(`stores/${encodeURIComponent(sid)}${query}`);
    if (seq !== state.detailSeq) return;
    state.data = data;
    renderDetail();
    if (state.view === "owners") await loadMembers();
  } catch (error) {
    if (seq === state.detailSeq) detailPlaceholder(error.message || "无法读取这家店铺的数据。");
  } finally {
    if (seq === state.detailSeq) $("detail").setAttribute("aria-busy", "false");
  }
}

function metricValue(key, data) {
  const value = data.metrics?.[key];
  return ["sales", "spend"].includes(key) ? money(value, data.currency_codes?.[key]) : scalar(value);
}

function kpiValue(kpi) {
  if (["CTR", "CDR", "TCOAS", "TACOS", "ACOS"].includes(kpi.code)) {
    return Number.isFinite(kpi.value) ? percent.format(kpi.value) : "—";
  }
  if (["CPC", "CPS", "CPA"].includes(kpi.code)) return money(kpi.value, kpi.currency);
  if (kpi.code === "ROAS" && Number.isFinite(kpi.value)) return `${scalar(kpi.value)} 倍`;
  return scalar(kpi.value);
}

function businessValue(value, unit, currency) {
  if (!Number.isFinite(value)) return "—";
  if (unit === "rate" || unit === "expense_rate") return `${moneyNumber.format(value * 100)}%`;
  if (unit === "money") {
    if (!/^[A-Z]{3}$/.test(currency || "")) return "币种待确认";
    return currency === "USD" ? `$${moneyNumber.format(value)}` : money(value, currency);
  }
  return scalar(value);
}

function businessComparison(item, warning) {
  if (warning) return "待确认";
  if (item.comparable === false) return "币种不同";
  if (!Number.isFinite(item.change)) {
    return item.previous === 0 && Number.isFinite(item.current) ? "前值为零" : "—";
  }
  const signed = `${item.change > 0 ? "+" : ""}${moneyNumber.format(item.change * 100)}`;
  return signed + (item.unit === "rate" || item.unit === "expense_rate" ? " 个百分点" : "%");
}

function renderBusinessDaily(data) {
  const business = data.business_daily;
  const items = (business?.metrics || []).filter((item) => Number.isFinite(item.current) &&
    (item.unit !== "money" || /^[A-Z]{3}$/.test(item.currency || "")));
  $("business-daily").hidden = !business && typeof data.business_source_configured !== "boolean";
  $("business-daily-table").hidden = !items.length;
  $("business-daily-period").textContent = items.length ? [
    `美国统计日：${business.report_date}`, `对比 ${business.previous_date}`, business.currency === "USD" ? "美元 USD" : business.currency,
  ].filter(Boolean).join(" · ") : "";
  tableRows("business-daily-rows", items.map((item) => [item.label,
    businessValue(item.current, item.unit, item.currency),
    businessValue(item.previous, item.unit, item.previous_currency),
    businessComparison(item, data.data_warning),
  ]), 4, "");
  const notes = [];
  if (business?.availability === "FAILED") notes.push("该日经营看板刷新失败，旧值不参与本次比较；请在后台重试同步。");
  else if (!items.length) notes.push(data.business_source_configured ? "经营看板暂无该日报数据。" : "经营看板数据尚未接入。");
  else {
    if (items.some((item) => item.unit === "expense_rate")) notes.push("¹ 领星经营看板口径，费用与退款占比按扣款绝对值展示。");
  }
  $("business-daily-note").textContent = notes.join(" ");
  // The source sales summary remains visible until the distinct business dashboard is available.
  $("metric-summary").hidden = items.length > 0;
}

function renderSourceObservations(data) {
  const observations = data.source_observations || {};
  const sales = observations.store_sales;
  const rows = [], bases = new Set();
  const api = data.api_business_observations || {};
  if (data.business_daily?.availability !== "AVAILABLE") {
    for (const [source, key, label, monetary] of [
      ["product", "volume", "销量（接口下单）", false], ["product", "netAmount", "净销售额（接口下单）", true],
      ["settlement", "platformIncome", "平台收入（接口结算）", true], ["settlement", "grossProfit", "毛利润（接口结算）", true],
      ["settlement", "grossRate", "毛利率（接口结算）", false], ["settlement", "refundsRate", "退款率（结算销量）", false],
    ]) {
      const item = api[source] || {}, value = item.metrics?.[key];
      if (!Number.isFinite(value) || monetary && !/^[A-Z]{3}$/.test(item.currency || "")) continue;
      rows.push([label, monetary ? money(value, item.currency) : key.endsWith("Rate") ? `${(value * 100).toFixed(2)}%` : scalar(value)]);
      if (item.basis) bases.add(item.basis);
    }
    const p = api.product || {};
    if (Number.isFinite(p.metrics?.adSpend) && p.metrics?.netAmount > 0) {
      rows.push(["广告费/净销售额（接口下单）", `${(Math.abs(p.metrics.adSpend) / p.metrics.netAmount * 100).toFixed(2)}%`]);
    }
  }
  if (sales && sales.metrics) {
    for (const [key, label] of [["sales", "销售额"], ["units", "销量"], ["orders", "订单"]]) {
      const value = sales.metrics[key];
      if (!Number.isFinite(value) || Number.isFinite(data.metrics?.[key])) continue;
      if (key === "sales" && !/^[A-Z]{3}$/.test(sales.currency || "")) continue;
      rows.push([label + "（下单口径）", key === "sales" ? money(value, sales.currency) : scalar(value)]);
      if (sales.basis) bases.add(sales.basis);
    }
  }
  for (const ad of observations.campaign_ads || []) {
    const original = (data.ad_breakdown || []).find((item) => item.type === String(ad.ad_type).toUpperCase());
    for (const [key, label] of [["spend", "花费"], ["clicks", "点击"], ["impressions", "曝光"]]) {
      const value = ad.metrics?.[key];
      if (!Number.isFinite(value) || Number.isFinite(original?.[key])) continue;
      if (key === "spend" && !/^[A-Z]{3}$/.test(ad.currency || "")) continue;
      rows.push([String(ad.ad_type).toUpperCase() + " · " + label, key === "spend" ? money(value, ad.currency) : scalar(value)]);
      if (ad.basis) bases.add(ad.basis);
    }
  }
  $("source-observations").hidden = !rows.length;
  $("source-observations-basis").textContent = [observations.report_date || api.report_date, ...bases].filter(Boolean).join(" · ");
  tableRows("source-observation-rows", rows, 2, "");
}

function renderDetail() {
  const data = state.data;
  const store = data.store || {};
  $("store-name").textContent = store.name || state.sid;
  $("store-eyebrow").textContent = [store.country, `店铺 ${state.sid}`].filter(Boolean).join(" · ");
  const ads = store.ads_authorized === true ? "广告已授权" : "广告授权待核实";
  $("store-meta").textContent = [store.timezone || "报表时区待确认", ads].join(" · ");
  $("report-title").textContent = data.display_reporting_date ? `${data.display_reporting_date} 日报` : "日报";
  const reportDate = $("report-date");
  reportDate.value = data.report_date || "";
  reportDate.max = data.latest_completed_date || "";
  const reportNotes = [];
  if (data.requested_date && data.report_date && data.requested_date !== data.report_date) {
    reportNotes.push(`查询日 ${data.requested_date} 暂无资料，当前展示 ${data.report_date}。`);
  }
  if (data.latest_completed_date) reportNotes.push(`最近已结束报表日 ${data.latest_completed_date}`);
  if (data.data_warning) reportNotes.push(data.data_warning);
  $("report-note").textContent = reportNotes.join(" · ");
  $("metric-summary").replaceChildren(...figures.filter(([key]) => Number.isFinite(data.metrics?.[key]) &&
    (!["sales", "spend"].includes(key) || /^[A-Z]{3}$/.test(data.currency_codes?.[key] || ""))).map(([key, label]) => {
    const tile = node("div", undefined, "metric");
    const value = metricValue(key, data);
    const dd = node("dd", value);
    if (value === "—") dd.setAttribute("aria-label", `${label}暂无可用值`);
    tile.append(node("dt", label), dd);
    return tile;
  }));
  renderBusinessDaily(data);
  renderSourceObservations(data);
  tableRows("kpi-rows", (data.kpis || []).filter((r) => Number.isFinite(r.value) &&
    (!["CPC", "CPS", "CPA"].includes(r.code) || /^[A-Z]{3}$/.test(r.currency || ""))).map((r) => [r.label || r.code, kpiValue(r), r.date,
    /数据不足|待确认|待核实|缺少|币种/.test(r.status || "") ? node("span", "") : badge(r.status)]), 4, "尚无可展示的广告指标。");
  tableRows("coverage-rows", (data.coverage || []).map((r) => [r.name, badge(r.status), scalar(r.rows)]), 3, "暂无采集记录。");
  const health = data.health || {};
  const healthItems = [];
  if (health.status && !/待确认|待核实|未知/.test(health.status)) healthItems.push(["当前状态", badge(health.status)]);
  if (Number.isFinite(health.score)) healthItems.push(["健康评分（观测）", scalar(health.score)]);
  if (health.source_rating && !/未知|未提供|未识别/.test(health.source_rating)) healthItems.push(["源评级（观测）", health.source_rating]);
  if (health.updated_at) healthItems.push(["源更新时间（原值）", health.updated_at]);
  for (const evidence of health.indicators || []) {
    if (!Number.isFinite(evidence.value)) continue;
    const observed = `${scalar(evidence.value)}%`;
    const standard = Number.isFinite(evidence.standard) ? `平台标准 ${scalar(evidence.standard)}%` : "";
    const value = [observed, standard, evidence.status_label || evidence.judgment].filter(Boolean).join(" · ");
    if (evidence.label || evidence.name) healthItems.push([evidence.label || evidence.name, value]);
  }
  $("health-data").replaceChildren(...healthItems.map(([label, value]) => {
    const div = node("div");
    const dd = node("dd");
    if (value instanceof Node) dd.append(value);
    else dd.textContent = display(value);
    div.append(node("dt", label), dd);
    return div;
  }));
  $("health-panel").hidden = !healthItems.length;
  const adRows = (data.ad_breakdown || []).filter((r) => Number.isFinite(r.spend) && /^[A-Z]{3}$/.test(r.currency || ""));
  $("ad-breakdown").hidden = !adRows.length;
  tableRows("ad-breakdown-rows", adRows.map((r) => [r.name || r.type, money(r.spend, r.currency),
    scalar(r.orders), scalar(r.clicks), scalar(r.impressions), badge(r.status)]), 6, "暂无广告类型数据。");
  $("recent-alerts").hidden = !Array.isArray(data.recent_alerts);
  tableRows("alert-rows", (data.recent_alerts || []).map((r) => [r.title, badge(r.status), r.date]), 3, "暂无近期异常记录。");
  tableRows("trend-rows", (data.trend || []).map((r) => [r.date, scalar(r.orders), money(r.sales, r.sales_currency),
    money(r.spend, r.spend_currency)]), 4, "暂无可用趋势数据。");
  renderTrend();
  renderRouting();
  renderControls();
  renderCollection();
  $("detail-placeholder").hidden = true;
  $("detail-content").hidden = false;
  renderView();
}

function validControls(controls) {
  return controls && typeof controls.collection_enabled === "boolean"
    && typeof controls.notifications_enabled === "boolean" && /^[0-9a-f]{64}$/.test(controls.revision || "");
}

function renderCollection() {
  const receipt = state.data?.collection;
  const active = receipt && ["PENDING", "RUNNING"].includes(receipt.status);
  const controls = state.data?.controls;
  $("sync-latest").disabled = state.collectionBusy || active || !state.csrf
    || !validControls(controls) || !controls.collection_enabled;
  $("collection-refresh").disabled = state.collectionBusy || !state.data;
  if (!receipt || receipt.status === "NONE") { notice("collection-status"); return; }
  const parts = [receipt.status_label];
  if (receipt.report_date) parts.push(`实际报表日 ${receipt.report_date}`);
  if (receipt.retry_at) {
    const stamp = new Date(`${receipt.retry_at}Z`);
    if (!Number.isNaN(stamp.getTime())) parts.push(`最早重试 ${stamp.toLocaleTimeString("zh-CN")}`);
  }
  if (["SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"].includes(receipt.status)) parts.push("可点击回到最新查看数据");
  notice("collection-status", parts.join(" · "), /FAILED|BLOCKED/.test(receipt.status) ? "error" : "info");
}

async function collectionStatus({ start = false, polls = 0 } = {}) {
  if (!state.sid || !state.data || state.collectionBusy) return;
  const sid = state.sid;
  const seq = state.collectionSeq;
  state.collectionBusy = true;
  let failed = false;
  clearTimeout(state.collectionTimer);
  renderCollection();
  try {
    const options = start ? { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: "{}" } : undefined;
    const receipt = await api(`stores/${encodeURIComponent(sid)}/collection`, options);
    if (seq !== state.collectionSeq || sid !== state.sid) return;
    state.data.collection = receipt;
    if (["PENDING", "RUNNING"].includes(receipt.status) && polls < 30) {
      state.collectionTimer = setTimeout(() => void collectionStatus({ polls: polls + 1 }), 10000);
    }
  } catch (error) {
    failed = true;
    if (seq === state.collectionSeq) notice("collection-status", error.message || "暂时无法同步，请稍后重试。", "error");
  } finally {
    if (seq === state.collectionSeq) {
      state.collectionBusy = false;
      // Preserve an explicit request error until the next successful status read.
      const receipt = state.data?.collection;
      if (!failed && receipt && receipt.status !== "NONE") renderCollection();
      else {
        $("sync-latest").disabled = !state.csrf || ["PENDING", "RUNNING"].includes(receipt?.status)
          || !validControls(state.data?.controls) || !state.data.controls.collection_enabled;
        $("collection-refresh").disabled = false;
      }
    }
  }
}

function controlBadges(controls) {
  if (!validControls(controls)) return [badge("开关待核实")];
  return [["采集", controls.collection_enabled], ["通知", controls.notifications_enabled]].map(([label, enabled]) =>
    node("span", `${label}${enabled ? "开启" : "已关闭"}`, `badge control-badge ${enabled ? "active" : "paused"}`));
}

function storeControlsChanged() {
  const saved = state.data?.controls;
  const draft = state.controlsDraft;
  return validControls(saved) && draft && (draft.collection_enabled !== saved.collection_enabled
    || draft.notifications_enabled !== saved.notifications_enabled);
}

function updateControls() {
  const controls = state.data?.controls;
  const ready = validControls(controls) && state.controlsDraft !== null;
  const busy = state.controlsSaving || state.controlsLoading;
  $("controls-field").disabled = !ready || busy;
  $("reload-controls").disabled = busy || !state.data;
  $("save-controls").disabled = !ready || busy || !state.csrf || !storeControlsChanged();
  for (const [name, input, label] of [["collection_enabled", "control-collection", "collection-state"],
    ["notifications_enabled", "control-notifications", "notifications-state"]]) {
    $(input).checked = ready && state.controlsDraft[name];
    $(label).textContent = !ready ? "待核实" : state.controlsDraft[name] ? "开启" : "关闭";
  }
  $("controls-save-note").textContent = !ready ? "无法核实开关状态，请刷新重试。"
    : state.controlsSaving ? "正在保存…" : state.controlsLoading ? "正在读取…"
      : storeControlsChanged() ? "修改尚未保存" : "设置已保存；后续任务按此执行。";
}

function renderControls() {
  const controls = state.data?.controls;
  state.controlsDraft = validControls(controls) ? {
    collection_enabled: controls.collection_enabled, notifications_enabled: controls.notifications_enabled,
  } : null;
  $("store-control-badges").replaceChildren(...controlBadges(controls));
  notice("controls-status", validControls(controls) ? "" : "开关状态暂时不可用，历史数据仍可查看。", "error");
  updateControls();
  renderCollection();
}

function acceptControls(sid, controls) {
  const row = state.stores.find((store) => String(store.sid) === sid);
  if (row) row.controls = controls;
  renderStoreList();
}

async function reloadControls({ conflict = false } = {}) {
  if (!state.data || state.controlsLoading || (state.controlsSaving && !conflict)) return;
  const details = state.data;
  const sid = state.sid;
  const seq = ++state.controlsSeq;
  state.controlsLoading = true;
  updateControls();
  notice("controls-status", "正在读取最新开关状态…");
  const query = details.requested_date || details.report_date;
  try {
    const response = await api(`stores/${encodeURIComponent(sid)}${query ? `?date=${encodeURIComponent(query)}` : ""}`);
    if (seq !== state.controlsSeq || state.data !== details) return;
    if (!validControls(response.controls)) throw new Error("无法核实开关状态，请稍后重试。");
    state.data.controls = response.controls;
    acceptControls(sid, response.controls);
    renderControls();
    notice("controls-status", conflict ? "开关已被其他页面更新，已载入最新设置。请重新选择后保存。" : "已载入最新开关状态。", conflict ? "error" : "success");
  } catch (error) {
    if (seq === state.controlsSeq && state.data === details) {
      // An unknown current revision must never be submitted after a conflict.
      state.controlsDraft = null;
      notice("controls-status", error.message || "无法读取最新开关状态。", "error");
    }
  } finally {
    if (seq === state.controlsSeq) {
      state.controlsLoading = false;
      updateControls();
    }
  }
}

async function saveControls() {
  if (!state.data || !validControls(state.data.controls) || $("save-controls").disabled) return;
  const sid = state.sid;
  const details = state.data;
  const payload = { ...state.controlsDraft, revision: details.controls.revision };
  state.controlsSaving = true;
  updateControls();
  notice("controls-status", "正在保存店铺开关…");
  try {
    const controls = await api(`stores/${encodeURIComponent(sid)}/controls`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify(payload),
    });
    if (!validControls(controls)) throw new Error("无法确认保存结果，请刷新开关核实。");
    acceptControls(sid, controls);
    if (state.data === details) {
      state.data.controls = controls;
      renderControls();
      notice("controls-status", "店铺开关已保存。", "success");
    } else notice("global-status", `店铺 ${sid} 的开关已保存。`, "success");
  } catch (error) {
    if (state.data === details && error.status === 409) await reloadControls({ conflict: true });
    else notice(state.data === details ? "controls-status" : "global-status", error.message || "开关保存失败。", "error");
  } finally {
    state.controlsSaving = false;
    updateControls();
  }
}

function chartElement(tag, attrs = {}, label) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, String(value));
  if (label !== undefined) el.textContent = label;
  return el;
}

function renderTrend() {
  const rows = [...(state.data?.trend || [])].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const key = state.metric;
  const label = figures.find((item) => item[0] === key)?.[1] || "趋势";
  const monetary = key !== "orders";
  const chart = $("trend-chart");
  const currencies = new Set(rows.filter((r) => Number.isFinite(r[key]) && r[`${key}_currency`]).map((r) => r[`${key}_currency`]));
  const value = (row) => Number.isFinite(row[key]) && (!monetary || row[`${key}_currency`]) ? row[key] : null;
  const values = rows.map(value).filter((v) => v !== null);
  const unit = monetary ? [...currencies][0] : "单";
  const note = currencies.size > 1 && monetary ? "比较区间包含不同币种，请查看趋势明细。"
    : values.length === 0 ? "暂无可绘制的采集记录。"
      : `${rows[0].date} 至 ${rows.at(-1).date} · 单位 ${unit}`;
  $("trend-note").textContent = note;
  chart.setAttribute("aria-label", `${label}趋势。${note}`);
  if (!values.length || (monetary && currencies.size > 1)) {
    chart.replaceChildren(node("p", note, "empty"));
    return;
  }
  const min = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const max = maximum === min ? 1 : maximum;
  const range = max - min;
  const width = 650;
  const left = 62;
  const right = width - 24;
  const top = 13;
  const bottom = 126;
  const chartSvg = chartElement("svg", { viewBox: `0 0 ${width} 155`, "aria-hidden": "true" });
  for (let tick = 0; tick <= 3; tick += 1) {
    const y = top + (bottom - top) * tick / 3;
    chartSvg.append(chartElement("line", { x1: left, x2: right, y1: y, y2: y, class: "chart-grid" }),
      chartElement("text", { x: left - 8, y: y + 4, "text-anchor": "end", class: "chart-label" }, scalar(max - range * tick / 3)));
  }
  const coordinateX = (index) => rows.length > 1 ? left + (right - left) * index / (rows.length - 1) : (left + right) / 2;
  const positions = [];
  let path = "";
  let previousDate = null;
  let before = false;
  rows.forEach((row, index) => {
    const numberValue = value(row);
    const currentDate = Date.parse(`${row.date}T00:00:00Z`);
    const consecutive = previousDate !== null && currentDate - previousDate === 86400000;
    previousDate = currentDate;
    if (numberValue === null) { before = false; return; }
    const x = coordinateX(index);
    const y = top + (bottom - top) * (max - numberValue) / range;
    path += `${before && consecutive ? "L" : "M"}${x} ${y} `;
    before = true;
    positions.push({ x, y, row, numberValue });
  });
  chartSvg.append(chartElement("path", { d: path, class: "chart-series" }));
  for (const point of positions) {
    const circle = chartElement("circle", { cx: point.x, cy: point.y, r: 4.2, class: "chart-point" });
    circle.append(chartElement("title", {}, `${point.row.date} ${label} ${monetary ? money(point.numberValue, unit) : scalar(point.numberValue)}`));
    chartSvg.append(circle);
  }
  const labelIndexes = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
  for (const idx of labelIndexes) {
    chartSvg.append(chartElement("text", { x: coordinateX(idx), y: 149, "text-anchor": "middle", class: "chart-label" }, String(rows[idx].date).slice(5)));
  }
  chart.replaceChildren(chartSvg);
}

function currentOwners() {
  return state.data?.routing?.owners || [];
}

function renderRouting() {
  const routing = state.data?.routing;
  const group = routing?.group;
  $("group-label").textContent = group ? `通知群：${group}` : "通知群待配置";
  const owners = currentOwners();
  const memberNames = new Map((state.members || []).map((member) => [member.id, member.name]));
  $("current-owners").replaceChildren(...(owners.length ? owners.map((item) => node("li", item.name || item.id))
    : [node("li", routing?.error ? "映射待核实" : "未映射负责人", "muted")]));
  owners.forEach((item, index) => {
    const memberName = memberNames.get(item.id);
    if (memberName) $("current-owners").children[index].textContent = memberName;
  });
}

function renderView() {
  const owners = state.view === "owners";
  const groups = state.view === "notification-group";
  const sources = state.view === "data-sources";
  const analysisSettings = state.view === "analysis-settings";
  $("page-title").textContent = analysisSettings ? "分析设置" : sources ? "领星数据源" : groups ? "通知群设置" : owners ? "负责人映射" : "经营概览";
  $("page-description").textContent = analysisSettings ? "管理 DeepSeek 模型与广告分析技能。" : sources ? "管理接口与 MCP 凭据、更换日期及操作记录。" : groups ? "管理接收日报和默认提醒的飞书群聊。" : owners ? "按店铺管理接收异常提醒的飞书负责人。" : "查看各店铺的销售、广告与账户绩效。";
  $("notification-group-panel").hidden = !groups;
  $("data-sources-panel").hidden = !sources;
  $("analysis-settings-panel").hidden = !analysisSettings;
  $("store-stats").hidden = groups || sources || analysisSettings;
  $("store-board").hidden = groups || sources || analysisSettings;
  $("bulk-sync-panel").hidden = groups || sources || analysisSettings;
  for (const tab of document.querySelectorAll("[data-view]")) {
    if (tab.dataset.view === state.view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  $("overview").hidden = owners;
  $("ownership").hidden = !owners;
  $("edit-owners").hidden = owners;
}

async function setView(view) {
  if (view !== "analysis-settings") $("deepseek-key").value = "";
  state.view = view;
  renderView();
  if (view === "owners" && state.data && !state.members) await loadMembers();
  if (view === "notification-group" && !state.groups.current) await loadGroups();
  if (view === "notification-group" && !state.adSubscribers.data) await loadAdSubscribers();
  if (view === "data-sources" && !state.sources.data) await loadSources();
  if (view === "analysis-settings") {
    if (!state.reviewSkills && window.JxReviewSkills) {
      state.reviewSkills = window.JxReviewSkills.mount($("review-skills-panel"), { api, csrf: () => state.csrf });
    }
    if (!state.deepseek.data) await loadDeepSeek();
  }
}

function setDeepSeekBusy() {
  const settings = state.deepseek;
  $("deepseek-fields").disabled = settings.loading || settings.saving || !settings.data?.revision || !state.csrf;
}

function renderDeepSeek() {
  const data = state.deepseek.data;
  const label = { CONFIGURED: "已配置（未检测可用性）", NOT_CONFIGURED: "未配置", INVALID_CONFIGURATION: "配置待核对" }[data?.status];
  $("deepseek-state").replaceChildren(...(label ? [badge(label)] : []));
  const meta = $("deepseek-meta");
  meta.replaceChildren();
  if (data?.updated_at) {
    const when = new Date(data.updated_at);
    if (Number.isFinite(when.getTime())) {
      const entry = node("div");
      entry.append(node("dt", "最后更新"), node("dd", when.toLocaleString("zh-CN")));
      meta.append(entry);
    }
  }
  $("deepseek-model").replaceChildren(...(data?.models || []).map((model) => {
    const option = node("option", model);
    option.value = model;
    return option;
  }));
  if (data?.model) $("deepseek-model").value = data.model;
  $("deepseek-timeout").value = data?.timeout ?? "";
  $("deepseek-key").required = data?.status === "NOT_CONFIGURED" || data?.status === "INVALID_CONFIGURATION";
  setDeepSeekBusy();
}

async function loadDeepSeek() {
  const settings = state.deepseek;
  if (settings.loading || settings.saving) return;
  settings.loading = true;
  $("deepseek-key").value = "";
  setDeepSeekBusy();
  try {
    settings.data = await api("deepseek");
    notice("deepseek-status");
  } catch (error) {
    settings.data = null;
    notice("deepseek-status", error.message, "error");
  } finally {
    settings.loading = false;
    renderDeepSeek();
  }
}

async function saveDeepSeek() {
  const settings = state.deepseek;
  if (settings.loading || settings.saving || !settings.data?.revision || !state.csrf) return;
  settings.saving = true;
  setDeepSeekBusy();
  try {
    settings.data = await api("deepseek", { method: "POST", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": state.csrf,
    }, body: JSON.stringify({
      revision: settings.data.revision, api_key: $("deepseek-key").value,
      model: $("deepseek-model").value, timeout: Number($("deepseek-timeout").value),
    }) });
    notice("deepseek-status", "DeepSeek 设置已保存，后续分析会读取新设置。", "success");
  } catch (error) {
    if (error.status === 409) settings.data = null;
    notice("deepseek-status", error.message, "error");
  } finally {
    $("deepseek-key").value = "";
    settings.saving = false;
    renderDeepSeek();
  }
}

function sourceTime(value) {
  if (typeof value !== "string") return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return "";
  const instant = new Date(value);
  return Number.isFinite(instant.getTime()) ? new Intl.DateTimeFormat("zh-CN", {
    timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(instant) : "";
}

function nextSourceMonth(today) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(today || "");
  if (!match) return "";
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const last = new Date(Date.UTC(year, month + 1, 0));
  const result = new Date(Date.UTC(year, month, Math.min(day, last.getUTCDate())));
  return result.toISOString().slice(0, 10);
}

function renderSources(data) {
  const busy = state.sources.loading || state.sources.saving;
  const dateParts = new Intl.DateTimeFormat("en", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const parts = Object.fromEntries(dateParts.map((part) => [part.type, part.value]));
  const today = `${parts.year}-${parts.month}-${parts.day}`;
  for (const kind of ["api", "mcp"]) {
    const info = data?.[kind] || {};
    $("source-" + kind + "-fields").disabled = busy || !state.csrf || !data?.revision;
    const status = { CONFIGURED: "已配置", EXPIRING: "待更换（7日内）", EXPIRED: "已到更换日期", INVALID: "配置需更新", NOT_CONFIGURED: "未配置" }[info.status];
    $("source-" + kind + "-state").replaceChildren(...(status ? [badge(status)] : []));
    const rows = [["服务地址", info.origin], ["添加 / 更新", sourceTime(info.updated_at)],
      ["计划更换", sourceTime(info.expires_on)]].filter(([, value]) => value);
    $("source-" + kind + "-meta").replaceChildren(...rows.map(([label, value]) => {
      const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair;
    }));
    const expiry = $("source-" + kind + "-expiry");
    if (!expiry.value) expiry.value = info.expires_on || nextSourceMonth(today);
    if (kind === "api") $("source-api-app-id").required = !info.configured;
  }
  $("reload-sources").disabled = busy;
  const actions = { ADD: "添加", ROTATE: "更新", added: "添加", rotated: "更新", updated: "更新", add: "添加", rotate: "更新", update: "更新" };
  const events = (data?.events || []).filter((item) => ["api", "mcp"].includes(item.kind) && actions[item.action] && sourceTime(item.at));
  tableRows("source-history-rows", events.map((item) => [item.kind === "api" ? "领星接口" : "领星 MCP",
    actions[item.action], sourceTime(item.at), item.expires_on || ""]), 4, "尚无添加或更换记录。");
}

async function loadSources() {
  if (state.sources.loading || state.sources.saving) return;
  state.sources.loading = true;
  renderSources(state.sources.data);
  try {
    state.sources.data = await api("data-sources");
    for (const kind of ["api", "mcp"]) $("source-" + kind + "-expiry").value = "";
    for (const id of ["source-api-app-id", "source-mcp-key", "source-api-secret", "source-api-app-key", "source-api-access-token"]) $(id).value = "";
    notice("sources-status");
  } catch (error) {
    notice("sources-status", error.message, "error");
  } finally {
    state.sources.loading = false;
    renderSources(state.sources.data);
  }
}

async function saveSource(kind) {
  if (state.sources.saving || !state.sources.data?.revision || !state.csrf) return;
  const credentials = kind === "mcp" ? { url: $("source-mcp-url").value.trim(), key: $("source-mcp-key").value.trim() } : {
    app_id: $("source-api-app-id").value.trim(), app_secret: $("source-api-secret").value.trim(),
    app_key: $("source-api-app-key").value.trim(), access_token: $("source-api-access-token").value.trim(),
  };
  if (!state.sources.data[kind]?.configured && (kind === "mcp" ? !credentials.key :
      !credentials.app_id || (!credentials.app_secret && !credentials.access_token))) {
    notice("sources-status", "请填写该数据源的必要凭据。", "error"); return;
  }
  state.sources.saving = true;
  renderSources(state.sources.data);
  try {
    state.sources.data = await api("data-sources", { method: "POST", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": state.csrf,
    }, body: JSON.stringify({ revision: state.sources.data.revision, kind, credentials,
      expires_on: $("source-" + kind + "-expiry").value || null }) });
    $("source-" + kind + "-expiry").value = "";
    notice("sources-status", "配置已保存，后续采集读取新凭据。", "success");
  } catch (error) {
    notice("sources-status", error.message, "error");
    if (error.status === 409) state.sources.data = null;
  } finally {
    for (const id of ["source-mcp-key", "source-api-secret", "source-api-app-key", "source-api-access-token"]) $(id).value = "";
    state.sources.saving = false;
    renderSources(state.sources.data);
  }
}

function updateGroupSelection() {
  const group = state.groups;
  const busy = group.loading || group.saving;
  $("notification-group-panel").setAttribute("aria-busy", String(busy));
  $("notification-group-select").disabled = busy || !group.chats.length;
  $("reload-groups").disabled = busy;
  $("preview-group").disabled = busy || !group.current || !$("notification-group-select").value;
  $("confirm-group-change").disabled = busy || !group.preview;
  $("save-notification-group").disabled = busy || !group.preview || !$("confirm-group-change").checked || !state.csrf;
  $("current-notification-group").textContent = group.current?.name || (group.loading ? "正在读取…" : "暂时无法读取");
}

function clearGroupPreview() {
  state.groups.preview = null;
  $("confirm-group-change").checked = false;
  $("group-impact").hidden = true;
  updateGroupSelection();
}

async function loadGroups() {
  const group = state.groups;
  if (group.saving) return;
  const seq = ++group.seq;
  group.loading = true;
  group.current = null;
  group.chats = [];
  clearGroupPreview();
  notice("group-settings-status", "正在读取机器人已加入的群…");
  try {
    const current = await api("notification-group");
    if (seq !== group.seq) return;
    group.current = current;
    updateGroupSelection();
    const result = await api("notification-group/chats");
    if (seq !== group.seq) return;
    group.current = result;
    group.chats = result.chats || [];
    const options = group.chats.map((chat) => {
      const duplicate = group.chats.filter((item) => item.name === chat.name).length > 1;
      const option = node("option", `${chat.name}${duplicate ? ` · …${chat.id.slice(-6)}` : ""}${chat.id === result.chat_id ? "（当前）" : ""}`);
      option.value = chat.id;
      return option;
    });
    const placeholder = node("option", "请选择目标群");
    placeholder.value = "";
    $("notification-group-select").replaceChildren(placeholder, ...options);
    $("notification-group-select").value = "";
    notice("group-settings-status", group.chats.length ? "" : "未读取到机器人已加入的群，请添加机器人后刷新。");
  } catch (error) {
    if (seq === group.seq) notice("group-settings-status", error.message || "群列表读取失败。", "error");
  } finally {
    if (seq === group.seq) { group.loading = false; updateGroupSelection(); }
  }
}

async function previewGroup() {
  const group = state.groups;
  if (!group.current || group.loading || group.saving || !state.csrf) return;
  const chat = $("notification-group-select").value;
  if (!chat) return;
  group.loading = true;
  clearGroupPreview();
  notice("group-settings-status", "正在核对目标群和人员映射…");
  try {
    const result = await api("notification-group/preview", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ chat_id: chat, revision: group.current.revision }),
    });
    group.preview = result;
    $("group-impact-summary").textContent = `${result.previous_name} → ${result.name}；保留管理员 ${result.retained_administrators} 人，移除群外管理员 ${result.removed_administrators} 人，${result.outside_owner_count} 位已映射负责人不在目标群。`;
    $("group-impact").hidden = false;
    notice("group-settings-status");
  } catch (error) {
    notice("group-settings-status", error.message || "无法核对目标群。", "error");
  } finally { group.loading = false; updateGroupSelection(); }
}

async function saveNotificationGroup() {
  const group = state.groups;
  if (!group.preview || group.loading || group.saving || $("save-notification-group").disabled) return;
  group.saving = true;
  updateGroupSelection();
  notice("group-settings-status", "正在保存通知群…");
  try {
    const result = await api("notification-group", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ chat_id: group.preview.chat_id, revision: group.preview.revision, confirmed: true }),
    });
    group.current = result;
    clearGroupPreview();
    $("notification-group-select").value = "";
    notice("group-settings-status", `通知群已保存为「${result.name}」，后续通知使用新配置。`, "success");
    await loadStores();
    await loadAdSubscribers();
  } catch (error) {
    clearGroupPreview();
    notice("group-settings-status", error.message || "通知群保存失败。", "error");
  } finally { group.saving = false; updateGroupSelection(); }
}

function updateAdSubscribers() {
  const model = state.adSubscribers;
  const current = model.data?.recipients || [];
  const existing = current.map((row) => row.id);
  const selected = [...model.selected];
  const changed = existing.length !== selected.length || selected.some((uid) => !existing.includes(uid));
  const busy = model.loading || model.saving;
  $("current-ad-subscribers").replaceChildren(...(current.length === 1
    ? [node("li", current[0].name || current[0].id)]
    : [node("li", current.length > 1 ? "旧配置包含多人，请重新选择一人" : "未设置广告总负责人", "muted")]));
  $("reload-ad-subscribers").disabled = busy;
  $("clear-ad-subscribers").disabled = busy || !model.data || !selected.length;
  const verified = new Set((model.members || []).map((row) => row.id));
  const unverified = selected.some((uid) => !verified.has(uid));
  $("save-ad-subscribers").disabled = busy || !state.csrf || !model.data || !changed || selected.length > 1 || (selected.length > 0 && unverified);
  $("ad-subscriber-field").disabled = busy;
  $("ad-subscriber-note").textContent = busy ? "正在处理…" : selected.length ? "已选择总负责人。" : "未设置时不额外私聊。";
}

function renderAdSubscriberMembers() {
  const model = state.adSubscribers;
  const query = String($("ad-subscriber-search").value || "").trim().toLowerCase();
  const known = new Set((model.members || []).map((row) => row.id));
  const missing = (model.data?.recipients || []).filter((row) => !known.has(row.id)).map((row) => ({ ...row, unavailable: true }));
  $("ad-subscriber-members").replaceChildren(...[...missing, ...(model.members || [])]
    .filter((row) => (row.name || row.id).toLowerCase().includes(query)).map((member) => {
      const label = node("label", undefined, "member");
      const check = node("input"); check.type = "radio"; check.name = "ad-alert-lead";
      check.checked = model.selected.has(member.id);
      check.disabled = Boolean(member.unavailable);
      check.addEventListener("change", () => {
        if (check.checked) model.selected = new Set([member.id]);
        updateAdSubscribers();
      });
      label.append(check, node("span", member.name || member.id));
      if (member.unavailable) label.append(badge("成员待核实"));
      return label;
    }));
  updateAdSubscribers();
}

async function loadAdSubscribers(includeMembers = false) {
  const model = state.adSubscribers;
  if (model.loading || model.saving) return;
  model.loading = true;
  updateAdSubscribers();
  notice("ad-subscriber-status", includeMembers ? "正在读取通知群成员…" : "");
  try {
    const value = await api(`ad-alert-subscribers${includeMembers ? "/members" : ""}`);
    if (!value || !Array.isArray(value.recipients) || !/^[0-9a-f]{64}$/.test(value.revision || "")) throw new Error("私聊接收人配置无法核对，请刷新。");
    model.data = value;
    model.selected = new Set(value.recipients.length === 1 ? [value.recipients[0].id] : []);
    model.members = includeMembers ? value.members || [] : null;
    $("ad-subscriber-search").hidden = !includeMembers;
    $("ad-subscriber-field").hidden = !includeMembers;
    if (includeMembers) renderAdSubscriberMembers();
    notice("ad-subscriber-status");
  } catch (error) {
    notice("ad-subscriber-status", error.message || "无法读取私聊接收人。", "error");
  } finally { model.loading = false; updateAdSubscribers(); }
}

async function saveAdSubscribers() {
  const model = state.adSubscribers;
  if (model.loading || model.saving || $("save-ad-subscribers").disabled) return;
  model.saving = true; updateAdSubscribers();
  try {
    const result = await api("ad-alert-subscribers", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ open_ids: [...model.selected], revision: model.data.revision }),
    });
    model.data = result;
    model.selected = new Set((result.recipients || []).map((row) => row.id));
    if (state.groups.current) state.groups.current.revision = result.revision;
    clearGroupPreview();
    notice("ad-subscriber-status", "广告总负责人已保存，不会补发旧告警。", "success");
  } catch (error) {
    notice("ad-subscriber-status", error.message || "保存失败，请刷新配置后重试。", "error");
  } finally { model.saving = false; updateAdSubscribers(); }
}

function ownerIDs() {
  return currentOwners().map((owner) => owner.id);
}

function updateSelection() {
  const existing = ownerIDs();
  const selected = [...state.selected];
  const changed = existing.length !== selected.length || existing.some((id) => !state.selected.has(id));
  const memberIDs = new Set((state.members || []).map((member) => member.id));
  const unverified = selected.filter((id) => !memberIDs.has(id)).length;
  const routing = state.data?.routing;
  const cannotClear = selected.length === 0 && routing?.fallback_enabled === false;
  const disabled = !state.members || state.saving;
  $("members-field").disabled = disabled;
  $("member-search").disabled = disabled;
  $("save-owners").disabled = disabled || !changed || !state.revision || unverified > 0 || cannotClear;
  $("selection-label").textContent = !state.members ? "未载入群成员" : unverified
    ? `已选择 ${selected.length} 人 · ${unverified} 人待核实，请调整后保存`
    : cannotClear ? "请选择至少一位负责人。"
      : `已选择 ${selected.length} 人${selected.length === 0 && routing?.fallback_group
        ? ` · 保存后发往 ${routing.fallback_group}` : ""}`;
}

function renderMembers() {
  const query = $("member-search").value.trim().toLowerCase();
  const members = state.members || [];
  const ids = new Set(members.map((member) => member.id));
  const unavailable = currentOwners().filter((owner) => !ids.has(owner.id)).map((owner) => ({ ...owner, unavailable: true }));
  const visible = [...unavailable, ...members].filter((member) => String(member.name || member.id).toLowerCase().includes(query));
  $("members").replaceChildren(...visible.map((member) => {
    const label = node("label", undefined, "member");
    const check = node("input");
    check.type = "checkbox";
    check.checked = state.selected.has(member.id);
    check.addEventListener("change", () => {
      if (check.checked) state.selected.add(member.id);
      else state.selected.delete(member.id);
      updateSelection();
    });
    label.append(check, node("span", member.name || member.id));
    if (member.unavailable) label.append(badge("群成员待核实"));
    return label;
  }));
  $("member-empty").hidden = !state.members || visible.length > 0;
  $("member-empty").textContent = query ? "没有符合条件的群成员。" : "此群暂无可选择的成员。";
  updateSelection();
}

async function loadMembers() {
  if (!state.data || state.saving) return;
  const seq = ++state.memberSeq;
  const sid = state.sid;
  state.members = null;
  state.revision = null;
  state.selected = new Set(ownerIDs());
  $("members").replaceChildren();
  $("member-empty").hidden = true;
  $("member-search").value = "";
  $("reload-members").disabled = true;
  updateSelection();
  if (state.data.routing?.error) {
    notice("mapping-status", state.data.routing.error, "error");
    $("reload-members").disabled = false;
    return;
  }
  notice("mapping-status", "正在读取通知群的成员…");
  try {
    const result = await api(`stores/${encodeURIComponent(sid)}/members`);
    if (seq !== state.memberSeq) return;
    if (result.revision !== state.data.routing?.revision) {
      notice("mapping-status", "映射已有更新，请刷新店铺数据后重新选择。", "error");
      return;
    }
    state.members = result.members || [];
    state.revision = result.revision;
    notice("mapping-status");
    renderRouting();
    renderMembers();
  } catch (error) {
    if (seq === state.memberSeq) notice("mapping-status", error.message || "无法读取群成员。", "error");
  } finally {
    if (seq === state.memberSeq) $("reload-members").disabled = false;
  }
}

async function saveOwners() {
  if (!state.data || !state.csrf || !state.revision || state.saving || $("save-owners").disabled) return;
  const sid = state.sid;
  const details = state.data;
  const selected = [...state.selected];
  state.saving = true;
  $("reload-members").disabled = true;
  updateSelection();
  notice("mapping-status", "正在保存负责人…");
  try {
    const result = await api(`stores/${encodeURIComponent(sid)}/owners`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ open_ids: selected, revision: state.revision }),
    });
    const ownerCount = (result.owners || []).length;
    const row = state.stores.find((s) => String(s.sid) === sid);
    if (row) row.owner_count = ownerCount;
    $("total-mapped").textContent = scalar(state.stores.filter((store) => store.owner_count > 0).length);
    renderStoreList();
    if (state.data === details) {
      state.data.routing = result;
      state.selected = new Set(ownerIDs());
      state.revision = result.revision;
      renderRouting();
      notice("mapping-status", "负责人映射已保存。", "success");
    } else notice("global-status", `店铺 ${sid} 的负责人映射已保存。`, "success");
  } catch (error) {
    notice(state.data === details ? "mapping-status" : "global-status", error.message || "负责人保存失败。", "error");
  } finally {
    state.saving = false;
    $("reload-members").disabled = false;
    updateSelection();
    if (state.data && state.data !== details && state.view === "owners") await loadMembers();
  }
}

$("stores").addEventListener("click", (event) => {
  const btn = event.target.closest("[data-sid]");
  if (btn) void selectStore(btn.dataset.sid);
});
$("store-search").addEventListener("input", renderStoreList);
$("reload-sources").addEventListener("click", () => void loadSources());
$("deepseek-form").addEventListener("submit", (event) => { event.preventDefault(); void saveDeepSeek(); });
for (const kind of ["api", "mcp"]) {
  $("source-" + kind + "-form").addEventListener("submit", (event) => { event.preventDefault(); void saveSource(kind); });
}
$("store-filter").addEventListener("change", renderStoreList);
$("refresh").addEventListener("click", () => {
  if (state.view === "analysis-settings") {
    void loadDeepSeek();
    void state.reviewSkills?.refresh();
  } else if (state.view === "data-sources") void loadSources();
  else if (state.view === "notification-group") void loadGroups();
  else void loadStores();
});
$("reload-groups").addEventListener("click", loadGroups);
$("notification-group-select").addEventListener("change", () => { clearGroupPreview(); notice("group-settings-status"); });
$("preview-group").addEventListener("click", previewGroup);
$("confirm-group-change").addEventListener("change", updateGroupSelection);
$("save-notification-group").addEventListener("click", saveNotificationGroup);
$("reload-ad-subscribers").addEventListener("click", () => loadAdSubscribers(true));
$("ad-subscriber-search").addEventListener("input", renderAdSubscriberMembers);
$("save-ad-subscribers").addEventListener("click", saveAdSubscribers);
$("clear-ad-subscribers").addEventListener("click", () => { state.adSubscribers.selected.clear(); renderAdSubscriberMembers(); });
for (const button of document.querySelectorAll("[data-view]")) {
  button.addEventListener("click", () => setView(button.dataset.view));
}
$("edit-owners").addEventListener("click", () => setView("owners"));
$("reload-members").addEventListener("click", loadMembers);
$("member-search").addEventListener("input", renderMembers);
$("save-owners").addEventListener("click", saveOwners);
for (const [id, key] of [["control-collection", "collection_enabled"], ["control-notifications", "notifications_enabled"]]) {
  $(id).addEventListener("change", () => {
    if (!state.controlsDraft || state.controlsSaving || state.controlsLoading) return;
    state.controlsDraft[key] = $(id).checked;
    notice("controls-status");
    updateControls();
  });
}
$("save-controls").addEventListener("click", saveControls);
$("reload-controls").addEventListener("click", () => reloadControls());
$("date-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.sid && $("report-date").value) void selectStore(state.sid, $("report-date").value);
});
$("latest-date").addEventListener("click", () => { if (state.sid) void selectStore(state.sid); });
$("sync-latest").addEventListener("click", () => { if (!$("sync-latest").disabled) void collectionStatus({ start: true }); });
$("collection-refresh").addEventListener("click", () => void collectionStatus());
$("sync-all").addEventListener("click", () => { if (!$("sync-all").disabled) void bulkCollectionStatus({ start: true }); });
$("bulk-collection-refresh").addEventListener("click", () => void bulkCollectionStatus());
for (const button of document.querySelectorAll("[data-metric]")) {
  button.addEventListener("click", () => {
    state.metric = button.dataset.metric;
    for (const option of document.querySelectorAll("[data-metric]")) option.setAttribute("aria-pressed", String(option === button));
    renderTrend();
  });
}
$("logout").disabled = false;
$("logout").addEventListener("click", async () => {
  if (!state.csrf) { location.assign("/console/login"); return; }
  $("logout").disabled = true;
  try {
    const response = await fetch("/console/logout", {
      method: "POST", headers: { "X-CSRF-Token": state.csrf }, credentials: "same-origin",
    });
    if (!response.ok && response.status !== 401) throw new Error("退出失败，请重试。");
    location.assign("/console/login");
  } catch (error) {
    notice("global-status", error.message || "退出失败，请重试。", "error");
    $("logout").disabled = false;
  }
});
void loadStores().then(() => bulkCollectionStatus());
