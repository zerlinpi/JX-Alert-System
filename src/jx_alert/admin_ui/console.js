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

async function selectStore(sid, date) {
  const seq = ++state.detailSeq;
  ++state.memberSeq;
  ++state.controlsSeq;
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

function renderDetail() {
  const data = state.data;
  const store = data.store || {};
  $("store-name").textContent = store.name || state.sid;
  $("store-eyebrow").textContent = [store.country, `店铺 ${state.sid}`].filter(Boolean).join(" · ");
  const ads = store.ads_authorized === true ? "广告已授权" : "广告授权待核实";
  $("store-meta").textContent = [store.timezone || "报表时区待确认", ads].join(" · ");
  $("report-title").textContent = data.report_date ? `${data.report_date} 日报` : "日报";
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
  $("metric-summary").replaceChildren(...figures.map(([key, label]) => {
    const tile = node("div", undefined, "metric");
    const value = metricValue(key, data);
    const dd = node("dd", value);
    if (value === "—") dd.setAttribute("aria-label", `${label}暂无可用值`);
    tile.append(node("dt", label), dd);
    return tile;
  }));
  tableRows("kpi-rows", (data.kpis || []).map((r) => [r.label || r.code, kpiValue(r), r.date, badge(r.status)]), 4, "暂无可用广告指标。");
  tableRows("coverage-rows", (data.coverage || []).map((r) => [r.name, badge(r.status), scalar(r.rows)]), 3, "暂无采集记录。");
  const health = data.health || {};
  const healthItems = [["当前状态", badge(health.status)], ["健康评分", scalar(health.score)], ["源评级", health.source_rating], ["源更新时间", health.updated_at]];
  if (health.reason) healthItems.push(["状态依据", health.reason]);
  for (const evidence of health.indicators || []) {
    const observed = Number.isFinite(evidence.value) ? `${scalar(evidence.value)}%` : "—";
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
  $("ad-breakdown").hidden = !Array.isArray(data.ad_breakdown);
  tableRows("ad-breakdown-rows", (data.ad_breakdown || []).map((r) => [r.name || r.type, money(r.spend, r.currency),
    scalar(r.orders), scalar(r.clicks), scalar(r.impressions), badge(r.status)]), 6, "暂无广告类型数据。");
  $("recent-alerts").hidden = !Array.isArray(data.recent_alerts);
  tableRows("alert-rows", (data.recent_alerts || []).map((r) => [r.title, badge(r.status), r.date]), 3, "暂无近期异常记录。");
  tableRows("trend-rows", (data.trend || []).map((r) => [r.date, scalar(r.orders), money(r.sales, r.sales_currency),
    money(r.spend, r.spend_currency)]), 4, "暂无可用趋势数据。");
  renderTrend();
  renderRouting();
  renderControls();
  $("detail-placeholder").hidden = true;
  $("detail-content").hidden = false;
  renderView();
}

function validControls(controls) {
  return controls && typeof controls.collection_enabled === "boolean"
    && typeof controls.notifications_enabled === "boolean" && /^[0-9a-f]{64}$/.test(controls.revision || "");
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
  $("page-title").textContent = owners ? "负责人映射" : "经营概览";
  $("page-description").textContent = owners ? "按店铺管理接收异常提醒的飞书负责人。" : "查看各店铺的销售、广告与账户绩效。";
  for (const tab of document.querySelectorAll("[data-view]")) {
    if (tab.dataset.view === state.view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  $("overview").hidden = owners;
  $("ownership").hidden = !owners;
  $("edit-owners").hidden = owners;
}

async function setView(view) {
  state.view = view;
  renderView();
  if (view === "owners" && state.data && !state.members) await loadMembers();
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
$("store-filter").addEventListener("change", renderStoreList);
$("refresh").addEventListener("click", loadStores);
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
void loadStores();
