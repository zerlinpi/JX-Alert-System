"use strict";
/* JX owner console. All provider and Feishu text is rendered with textContent only. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.JXConsole = api;
  if (typeof document !== "undefined" && typeof window !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => api.start(document, window));
    else api.start(document, window);
  }
})(typeof self !== "undefined" ? self : this, function () {
  const LANES = {
    daily: { label: "08:30 广告日报", when: "每天 08:30" },
    review: { label: "广告异常复盘", when: "每天 08:30（可选仅周一）" },
    hourly: { label: "实时监控", when: "每小时（发送时段内）" },
  };
  const OUTBOX = {
    DELIVERED: ["已送达", "ok"], PENDING: ["等待发送", ""], RETRY: ["等待重试", "warn"],
    PRE_SEND_RETRY: ["等待重试", "warn"], PREPARING: ["发送中", ""], SUBMITTING: ["发送中", ""],
    SENDING: ["发送中", ""], DELIVERY_UNKNOWN: ["可能已提交（不自动重发）", "warn"], SKIPPED: ["已跳过", ""],
    EXPIRED: ["已过发送时段", ""], DEAD: ["发送失败", "bad"], REJECTED: ["飞书拒绝", "bad"],
  };
  const RUNS = { SUCCEEDED: ["成功", "ok"], FAILED: ["失败", "bad"], SKIPPED: ["跳过", "warn"] };
  const NARRATIVE = { deepseek: "DeepSeek 撰写", rules: "系统规则", unavailable: "DeepSeek 不可用，规则兜底",
                      rejected: "DeepSeek 未通过核对，规则兜底" };
  const TOGGLES = { daily: ["enabled", "to_group", "to_lead", "ai"],
                    review: ["enabled", "to_group", "mention_operators", "to_lead", "to_operators", "ai", "weekly"],
                    hourly: ["enabled", "to_group", "mention_operators", "to_lead", "to_operators"] };
  const NUMBERS = ["start_hour", "end_hour", "high_spend", "low_spend", "min_clicks", "acos_high", "cvr_low",
                   "acos_good", "min_orders_good"];

  function statusLabel(status) {
    const found = OUTBOX[status];
    return found ? { text: found[0], tone: found[1] } : { text: status || "—", tone: "" };
  }

  function runLabel(status) {
    const found = RUNS[status];
    return found ? { text: found[0], tone: found[1] } : { text: status || "尚未运行", tone: "" };
  }

  function formatTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }

  function parseBold(text) {
    const parts = String(text || "").split("**");
    return parts.map((part, index) => ({ text: part, bold: index % 2 === 1 })).filter((part) => part.text);
  }

  function cardText(card) {
    if (!card || !card.header || !card.body) return "";
    const lines = [card.header.title.content];
    for (const element of card.body.elements || []) {
      if (element.tag === "markdown") lines.push(element.content);
      else if (element.tag === "table") {
        const names = element.columns.map((column) => column.name);
        lines.push(element.columns.map((column) => column.display_name).join(" | "));
        for (const row of element.rows) lines.push(names.map((name) => row[name]).join(" | "));
      } else if (element.tag === "hr") lines.push("————————");
    }
    return lines.join("\n");
  }

  function filterStores(rows, query) {
    const needle = String(query || "").trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => [row.name, row.short_name, row.country_label, row.profile_id,
      ...(row.operators || []).map((person) => person.name)].some((value) => String(value || "").toLowerCase().includes(needle)));
  }

  function lanePayload(values) {
    const lanes = {};
    for (const [lane, keys] of Object.entries(TOGGLES)) {
      lanes[lane] = {};
      for (const key of keys) lanes[lane][key] = Boolean(values[lane] && values[lane][key]);
    }
    for (const key of NUMBERS) {
      const raw = values.hourly ? values.hourly[key] : undefined;
      const number = Number(raw);
      if (raw === "" || raw === undefined || !Number.isFinite(number)) throw new Error("请填写完整的监控阈值。");
      lanes.hourly[key] = ["start_hour", "end_hour", "min_clicks", "min_orders_good"].includes(key)
        ? Math.trunc(number) : number;
    }
    if (lanes.hourly.start_hour >= lanes.hourly.end_hour) throw new Error("发送时段的开始须早于结束。");
    if (lanes.hourly.low_spend >= lanes.hourly.high_spend) throw new Error("低花费起算须小于高花费线。");
    return lanes;
  }

  function start(doc, win) {
    if (doc.documentElement.dataset.jxStarted) return;  // One set of listeners per page.
    doc.documentElement.dataset.jxStarted = "1";
    const $ = (id) => doc.getElementById(id);
    const csrf = doc.querySelector('meta[name="jx-console-csrf"]').content;
    const state = { stores: null, members: null, picker: null, lead: null, reports: null, recipients: null, groups: null };

    function el(tag, text, className) {
      const node = doc.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (className) node.className = className;
      return node;
    }

    function badge(label) {
      return el("span", label.text, "badge" + (label.tone ? " " + label.tone : ""));
    }

    function row(cells) {
      const tr = doc.createElement("tr");
      for (const cell of cells) {
        const td = doc.createElement("td");
        if (cell instanceof win.Node) td.append(cell); else td.textContent = cell === null || cell === undefined ? "—" : String(cell);
        tr.append(td);
      }
      return tr;
    }

    function fill(id, rows, empty, columns) {
      const body = $(id);
      body.replaceChildren(...rows);
      if (!rows.length) {
        const td = el("td", empty, "muted");
        td.colSpan = columns;
        const tr = doc.createElement("tr");
        tr.append(td);
        body.append(tr);
      }
    }

    function notice(id, text, tone) {
      const node = $(id);
      node.textContent = text || "";
      node.className = "notice" + (tone ? " " + tone : "");
      node.hidden = !text;
    }

    async function api(path, options) {
      const init = { method: (options && options.method) || "GET", credentials: "same-origin", headers: {} };
      if (init.method === "POST") {
        init.headers["Content-Type"] = "application/json";
        init.headers["X-CSRF-Token"] = csrf;
        init.body = JSON.stringify((options && options.body) || {});
      }
      const response = await win.fetch(path, init);
      let data = null;
      try { data = await response.json(); } catch (error) { data = null; }
      if (response.status === 401) { win.location.assign("/console/login"); throw new Error("登录已过期，请重新登录。"); }
      if (!response.ok) throw new Error((data && data.error) || "请求失败，请稍后重试。");
      return data;
    }

    async function guarded(statusId, action, busy) {
      if (busy) busy.disabled = true;
      try { return await action(); }
      catch (error) { notice(statusId, error.message, "error"); return null; }
      finally { if (busy) busy.disabled = false; }
    }

    // ---------------------------------------------------------------- overview
    function card(title, value, detail, tone) {
      const node = el("article", null, "card");
      node.append(el("h2", title));
      const strong = el("strong", value);
      if (tone) strong.append(" ", badge({ text: tone.text, tone: tone.tone }));
      node.append(strong, el("span", detail, "detail"));
      return node;
    }

    async function loadOverview() {
      const data = await guarded("global-status", () => api("/console/api/overview"));
      if (!data) return;
      notice("global-status", "");
      const sparkx = data.sparkx || {}, deepseek = data.deepseek || {}, feishu = data.feishu || {}, stores = data.stores || {};
      $("status-cards").replaceChildren(
        card("SparkX MCP", sparkx.configured ? "已配置" : "未配置",
          sparkx.configured ? (sparkx.token_source === "console" ? "Token 来自控制台" : "Token 来自环境变量") : "请在「SparkX 连接」保存 Token",
          sparkx.configured ? { text: "只读", tone: "ok" } : { text: "待配置", tone: "warn" }),
        card("DeepSeek", deepseek.configured ? (deepseek.model || "已配置") : "未配置",
          deepseek.configured ? "撰写须知、复盘建议与飞书问答" : "未配置时使用系统规则文字",
          deepseek.configured ? null : { text: "待配置", tone: "warn" }),
        card("飞书发送", feishu.delivery_enabled ? "已开启" : (feishu.dry_run ? "演练模式（不发送）" : "未开启"),
          "通知群：" + (feishu.group || "未设置") + (feishu.bot_enabled ? " · 问答机器人已开启" : ""),
          feishu.delivery_enabled ? { text: "正常", tone: "ok" } : { text: "不发送", tone: "warn" }),
        card("店铺", `${stores.monitored || 0} / ${stores.authorized || 0}`,
          `监控 / Token 授权；${stores.mapped || 0} 家已指定运营；店铺列表更新于 ${formatTime(stores.roster_at)}`));
      const lanes = data.lanes || {};
      fill("lane-rows", Object.entries(LANES).map(([key, meta]) => {
        const lane = lanes[key] || {}, last = lane.last || null;
        const result = last ? [runLabel(last.status).text, last.queued ? `排队 ${last.queued} 条` : "",
          last.narrative ? NARRATIVE[last.narrative] || "" : "", last.message || ""].filter(Boolean).join("；") : "—";
        return row([meta.label, meta.when, badge(lane.enabled ? { text: "开启", tone: "ok" } : { text: "关闭", tone: "" }),
          last ? formatTime(last.at) : "—", result]);
      }), "", 5);
      fill("recent-rows", (data.recent || []).map((item) => row([formatTime(item.created_at), item.lane, item.recipient,
        badge(statusLabel(item.status))])), "还没有发送记录。", 4);
    }

    // ---------------------------------------------------------------- stores
    function chips(people, emptyText) {
      const span = doc.createElement("span");
      if (!people || !people.length) { span.append(el("span", emptyText, "chip empty")); return span; }
      for (const person of people) span.append(el("span", person.name || person.id || person.open_id, "chip"));
      return span;
    }

    function renderStores() {
      const data = state.stores;
      if (!data) return;
      $("roster-time").textContent = "SparkX 店铺列表更新于 " + formatTime(data.roster_at);
      const rows = filterStores(data.rows, $("store-search").value).map((store) => {
        const name = doc.createElement("div");
        name.append(el("strong", store.short_name), el("div", store.name + (store.authorized ? "" : "（Token 已不再授权）"), "muted"));
        const monitor = doc.createElement("input");
        monitor.type = "checkbox";
        monitor.checked = store.monitored;
        monitor.setAttribute("aria-label", "监控 " + store.short_name);
        monitor.addEventListener("change", () => saveStore(store, { monitored: monitor.checked }, monitor));
        const people = doc.createElement("div");
        people.append(chips(store.operators, "未映射运营"));
        const edit = el("button", "编辑", "link-button");
        edit.type = "button";
        edit.addEventListener("click", () => openPicker(store));
        people.append(edit);
        let suggestion = "—";
        if (store.suggestion) {
          suggestion = doc.createElement("div");
          suggestion.append(chips(store.suggestion.operators.map((p) => ({ name: p.name })), ""),
            el("div", "匹配旧店铺：" + store.suggestion.store, "muted"));
        }
        return row([name, store.country_label, store.store_type === "vendor" ? "Vendor" : store.store_type === "seller" ? "Seller" : "—",
          monitor, people, suggestion]);
      });
      fill("store-rows", rows, data.rows.length ? "没有匹配的店铺。" : "还没有店铺：请先在「SparkX 连接」保存 Token，再点击「从 SparkX 刷新店铺」。", 6);
      $("apply-suggestions").disabled = !data.rows.some((store) => store.suggestion && store.authorized);
    }

    async function loadStores() {
      const data = await guarded("stores-status", () => api("/console/api/stores"));
      if (data) { state.stores = data; renderStores(); }
    }

    async function saveStore(store, change, control) {
      const operatorIds = (change.operator_ids || store.operators.map((person) => person.id));
      const body = { revision: state.stores.revision, changes: { [store.profile_id]: {
        monitored: change.monitored === undefined ? store.monitored : change.monitored, operator_ids: operatorIds } } };
      const data = await guarded("stores-status", () => api("/console/api/stores/save", { method: "POST", body }), control);
      if (data) { state.stores = data; renderStores(); notice("stores-status", "已保存 " + store.short_name + "。"); }
      else await loadStores();
      return data;
    }

    async function members(force) {
      if (!state.members || force) {
        const data = await api("/console/api/members");
        state.members = data.members || [];
      }
      return state.members;
    }

    function renderMembers(containerId, query, selected, type, name) {
      const container = $(containerId);
      const needle = String(query || "").trim().toLowerCase();
      const items = (state.members || []).filter((person) => !needle || person.name.toLowerCase().includes(needle));
      const legend = container.querySelector("legend");
      container.replaceChildren(...(legend ? [legend] : []), ...items.map((person) => {
        const label = el("label", null, "check");
        const input = doc.createElement("input");
        input.type = type; input.name = name; input.value = person.id; input.checked = selected.has(person.id);
        input.addEventListener("change", () => {
          if (type === "radio") { selected.clear(); selected.add(person.id); }
          else if (input.checked) selected.add(person.id); else selected.delete(person.id);
        });
        label.append(input, person.name);
        return label;
      }));
      if (!items.length) container.append(el("p", "没有匹配的群成员。", "muted"));
    }

    async function openPicker(store) {
      try { await members(); } catch (error) { notice("stores-status", error.message, "error"); return; }
      state.picker = { store, selected: new Set(store.operators.map((person) => person.id)) };
      $("picker-store").textContent = store.short_name + " · " + store.country_label;
      $("picker-search").value = "";
      renderMembers("picker-members", "", state.picker.selected, "checkbox", "operator");
      $("operator-picker").hidden = false;
      $("operator-picker").scrollIntoView({ block: "nearest" });
    }

    // ---------------------------------------------------------------- notifications
    function renderRecipients() {
      const data = state.recipients;
      $("current-group").textContent = (data.group_detail && data.group_detail.name) || data.group || "未设置";
      for (const kind of ["company", "ad"]) {
        const person = data[kind];
        const chip = $("lead-" + kind);
        chip.textContent = person ? person.name : "未设置";
        chip.className = "chip" + (person ? "" : " empty");
      }
    }

    function renderLanes() {
      const lanes = state.reports.lanes;
      for (const section of doc.querySelectorAll("#lanes-form [data-lane]")) {
        const lane = section.dataset.lane;
        for (const input of section.querySelectorAll("input")) {
          if (input.type === "checkbox") input.checked = Boolean(lanes[lane][input.name]);
          else input.value = lanes[lane][input.name];
        }
      }
      $("lanes-updated").textContent = state.reports.updated_at ? "上次保存：" + formatTime(state.reports.updated_at) : "尚未修改，使用默认设置（全部开启）";
    }

    async function loadNotifications() {
      const [recipients, reports] = await Promise.all([
        guarded("notifications-status", () => api("/console/api/recipients")),
        guarded("notifications-status", () => api("/console/api/reports"))]);
      if (recipients) { state.recipients = recipients; renderRecipients(); }
      if (reports) { state.reports = reports; renderLanes(); }
    }

    function formValues() {
      const values = {};
      for (const section of doc.querySelectorAll("#lanes-form [data-lane]")) {
        const lane = section.dataset.lane;
        values[lane] = {};
        for (const input of section.querySelectorAll("input")) {
          values[lane][input.name] = input.type === "checkbox" ? input.checked : input.value;
        }
      }
      return values;
    }

    function renderCard(container, value) {
      container.replaceChildren();
      if (!value) return;
      const header = el("header", value.header.title.content, value.header.template || "");
      const body = el("div", null, "card-body");
      for (const element of value.body.elements) {
        if (element.tag === "markdown") {
          for (const line of String(element.content).split("\n")) {
            const paragraph = doc.createElement("p");
            for (const part of parseBold(line)) paragraph.append(part.bold ? el("strong", part.text) : doc.createTextNode(part.text));
            body.append(paragraph);
          }
        } else if (element.tag === "table") {
          const wrap = el("div", null, "table-scroll");
          const table = doc.createElement("table");
          const head = doc.createElement("thead");
          const tr = doc.createElement("tr");
          for (const column of element.columns) tr.append(el("th", column.display_name));
          head.append(tr);
          const tbody = doc.createElement("tbody");
          for (const item of element.rows) tbody.append(row(element.columns.map((column) => item[column.name])));
          table.append(head, tbody);
          wrap.append(table);
          body.append(wrap);
        } else if (element.tag === "hr") body.append(doc.createElement("hr"));
      }
      container.append(header, body);
    }

    async function preview(lane, button) {
      notice("notifications-status", "正在从 SparkX 读取数据并生成预览（只读，不会发送）…");
      const data = await guarded("notifications-status", () => api("/console/api/reports/preview", { method: "POST", body: { lane } }), button);
      if (!data) return;
      notice("notifications-status", "");
      const info = data.info || {};
      $("preview-title").textContent = "预览 · " + LANES[lane].label;
      $("preview-info").textContent = [`监控店铺 ${info.profiles ?? "—"} 家`, info.report_date ? "数据日 " + info.report_date : "",
        info.window ? "近 7 日 " + info.window.join(" ~ ") : "", info.narrative ? NARRATIVE[info.narrative] || "" : "",
        info.errors && Object.keys(info.errors).length ? "部分数据缺失：" + Object.keys(info.errors).join("、") : ""].filter(Boolean).join(" · ");
      renderCard($("preview-card"), data.card);
      $("preview-text").textContent = data.text || "";
      $("preview-panel").hidden = false;
      $("preview-panel").scrollIntoView({ block: "start" });
    }

    // ---------------------------------------------------------------- sparkx & deepseek
    function facts(id, entries) {
      const list = $(id);
      list.replaceChildren();
      for (const [term, value] of entries) list.append(el("dt", term), el("dd", value));
    }

    async function loadSparkx() {
      const data = await guarded("sparkx-status", () => api("/console/api/sparkx"));
      if (!data) return;
      state.sparkx = data;
      facts("sparkx-facts", [["接口", data.endpoint], ["Token", data.configured ? (data.token_source === "console" ? "已保存（控制台）" : "来自环境变量") : "未配置"],
        ["更新时间", formatTime(data.updated_at)]]);
    }

    async function loadDeepseek() {
      const data = await guarded("deepseek-status", () => api("/console/api/deepseek"));
      if (!data) return;
      state.deepseek = data;
      facts("deepseek-facts", [["状态", data.configured ? "已配置" : "未配置"],
        ["密钥来源", data.key_source === "console" ? "控制台" : data.key_source === "environment" ? "环境变量" : data.key_source ? "私有文件" : "—"],
        ["更新时间", formatTime(data.updated_at)]]);
      $("deepseek-model").replaceChildren(...(data.models || []).map((model) => {
        const option = el("option", model); option.value = model; option.selected = model === data.model; return option;
      }));
      $("deepseek-timeout").value = data.timeout || 45;
    }

    async function loadDeliveries() {
      const data = await guarded("global-status", () => api("/console/api/deliveries"));
      if (!data) return;
      fill("delivery-rows", data.rows.map((item) => row([formatTime(item.created_at), item.lane, item.recipient, item.title,
        badge(statusLabel(item.status)), item.error || (item.delivered_at ? "送达 " + formatTime(item.delivered_at) : "—")])),
        "还没有发送记录。", 6);
    }

    // ---------------------------------------------------------------- navigation
    const loaders = { overview: loadOverview, stores: loadStores, notifications: loadNotifications, sparkx: loadSparkx,
                      deepseek: loadDeepseek, deliveries: loadDeliveries };

    function show(view) {
      for (const button of doc.querySelectorAll("[data-view]")) {
        if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
      }
      for (const panel of doc.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== view;
      notice("global-status", "");
      loaders[view]();
    }

    for (const button of doc.querySelectorAll("[data-view]")) button.addEventListener("click", () => show(button.dataset.view));
    for (const button of doc.querySelectorAll("[data-refresh]")) button.addEventListener("click", () => loaders[button.dataset.refresh]());
    $("logout").addEventListener("click", async () => {
      try { await win.fetch("/console/logout", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } }); }
      finally { win.location.assign("/console/login"); }
    });

    $("store-search").addEventListener("input", renderStores);
    $("refresh-roster").addEventListener("click", async (event) => {
      notice("stores-status", "正在读取 SparkX 授权店铺…");
      const data = await guarded("stores-status", () => api("/console/api/stores/refresh", { method: "POST" }), event.currentTarget);
      if (data) { state.stores = data; renderStores(); notice("stores-status", `已更新：${data.rows.length} 家店铺。`); }
    });
    $("apply-suggestions").addEventListener("click", async (event) => {
      const data = await guarded("stores-status", () => api("/console/api/stores/suggestions/apply", {
        method: "POST", body: { revision: state.stores.revision } }), event.currentTarget);
      if (data) {
        state.stores = data; renderStores();
        notice("stores-status", `已为 ${data.applied} 家店铺采用建议运营` + (data.skipped_people ? `；${data.skipped_people} 人已不在通知群，未采用。` : "。"));
      }
    });
    $("picker-search").addEventListener("input", () => renderMembers("picker-members", $("picker-search").value, state.picker.selected, "checkbox", "operator"));
    $("picker-close").addEventListener("click", () => { $("operator-picker").hidden = true; });
    $("picker-save").addEventListener("click", async (event) => {
      if (state.picker.selected.size > 10) { notice("stores-status", "每家店最多 10 位运营。", "error"); return; }
      const data = await saveStore(state.picker.store, { operator_ids: [...state.picker.selected] }, event.currentTarget);
      if (data) $("operator-picker").hidden = true;
    });

    $("load-groups").addEventListener("click", async (event) => {
      const data = await guarded("notifications-status", () => api("/console/api/groups"), event.currentTarget);
      if (!data) return;
      state.groups = data;
      $("group-select").replaceChildren(...data.chats.map((chat) => {
        const option = el("option", chat.name); option.value = chat.id; option.selected = chat.id === data.chat_id; return option;
      }));
      $("group-select").disabled = false;
      $("save-group").disabled = false;
    });
    $("save-group").addEventListener("click", async (event) => {
      if (!$("group-confirm").checked) { notice("notifications-status", "请先勾选确认更换范围。", "warn"); return; }
      const body = { chat_id: $("group-select").value, revision: state.groups.revision, confirmed: true };
      const data = await guarded("notifications-status", () => api("/console/api/groups", { method: "POST", body }), event.currentTarget);
      if (data) { notice("notifications-status", "通知群已保存：" + data.name); state.members = null; loadNotifications(); }
    });
    for (const button of doc.querySelectorAll("[data-lead]")) {
      button.addEventListener("click", async () => {
        try { await members(true); } catch (error) { notice("notifications-status", error.message, "error"); return; }
        const kind = button.dataset.lead, current = state.recipients[kind];
        state.lead = { kind, selected: new Set(current ? [current.id] : []) };
        $("lead-search").value = "";
        renderMembers("lead-members", "", state.lead.selected, "radio", "lead");
        $("lead-picker").hidden = false;
      });
    }
    $("lead-search").addEventListener("input", () => renderMembers("lead-members", $("lead-search").value, state.lead.selected, "radio", "lead"));
    async function saveLead(ids, button) {
      const body = { open_ids: ids, revision: state.recipients.revision };
      const data = await guarded("notifications-status", () => api("/console/api/recipients/" + state.lead.kind, { method: "POST", body }), button);
      if (data) { $("lead-picker").hidden = true; notice("notifications-status", "负责人已保存。"); loadNotifications(); }
    }
    $("lead-save").addEventListener("click", (event) => saveLead([...state.lead.selected].slice(0, 1), event.currentTarget));
    $("lead-clear").addEventListener("click", (event) => saveLead([], event.currentTarget));
    $("lanes-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      let lanes;
      try { lanes = lanePayload(formValues()); } catch (error) { notice("notifications-status", error.message, "error"); return; }
      const data = await guarded("notifications-status", () => api("/console/api/reports", {
        method: "POST", body: { revision: state.reports.revision, lanes } }), $("save-lanes"));
      if (data) { state.reports = data; renderLanes(); notice("notifications-status", "通知设置已保存，下次运行生效。"); }
    });
    for (const button of doc.querySelectorAll("[data-preview]")) button.addEventListener("click", () => preview(button.dataset.preview, button));
    $("preview-close").addEventListener("click", () => { $("preview-panel").hidden = true; });

    $("sparkx-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = $("sparkx-token").value.trim();
      if (!token) { notice("sparkx-status", "请填写新的 Token。", "warn"); return; }
      const data = await guarded("sparkx-status", () => api("/console/api/sparkx", { method: "POST", body: { revision: state.sparkx.revision, token } }));
      if (data) { $("sparkx-token").value = ""; notice("sparkx-status", "Token 已保存，可点击「测试连接」。"); loadSparkx(); }
    });
    $("sparkx-clear").addEventListener("click", async () => {
      const data = await guarded("sparkx-status", () => api("/console/api/sparkx", { method: "POST", body: { revision: state.sparkx.revision, clear: true } }));
      if (data) { notice("sparkx-status", "已清除控制台 Token（如环境变量中有 Token，将改用环境变量）。"); loadSparkx(); }
    });
    $("sparkx-test").addEventListener("click", async (event) => {
      notice("sparkx-status", "正在连接 mcp.sparkx.cn …");
      const data = await guarded("sparkx-status", () => api("/console/api/sparkx/test", { method: "POST" }), event.currentTarget);
      if (!data) return;
      if (data.status !== "OK") { notice("sparkx-status", data.message || ("连接失败：" + data.status), "error"); return; }
      notice("sparkx-status", `连接成功：Token 授权 ${data.profile_count} 家店铺。`);
      fill("sparkx-profiles", data.profiles.map((item) => row([item.profile_id, item.name, item.country, item.store_type || "—"])), "Token 没有授权任何店铺。", 4);
      $("sparkx-profiles-panel").hidden = false;
    });

    $("deepseek-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = { revision: state.deepseek.revision, api_key: $("deepseek-key").value.trim(),
                     model: $("deepseek-model").value, timeout: Number($("deepseek-timeout").value) };
      const data = await guarded("deepseek-status", () => api("/console/api/deepseek", { method: "POST", body }));
      if (data) { $("deepseek-key").value = ""; notice("deepseek-status", "DeepSeek 设置已保存。"); loadDeepseek(); }
    });
    $("deepseek-test").addEventListener("click", async (event) => {
      const data = await guarded("deepseek-status", () => api("/console/api/deepseek/test", { method: "POST" }), event.currentTarget);
      if (!data) return;
      const messages = { OK: "连接成功，模型可用。", NOT_CONFIGURED: "尚未配置密钥。", INVALID_KEY: "密钥无效。",
        INSUFFICIENT_BALANCE: "账户余额不足。", MODEL_UNAVAILABLE: "当前模型不可用，请更换模型。", TIMEOUT: "连接超时。",
        UNREACHABLE: "无法连接 api.deepseek.com。" };
      notice("deepseek-status", messages[data.status] || ("连接失败：" + data.status), data.status === "OK" ? "" : "error");
    });

    show("overview");
  }

  return { start, statusLabel, runLabel, formatTime, parseBold, cardText, filterStores, lanePayload, LANES };
});
