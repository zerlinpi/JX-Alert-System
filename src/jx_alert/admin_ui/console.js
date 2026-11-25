"use strict";
/* JX owner console. All provider, model and Feishu text is rendered with textContent only. */
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
    hourly: { label: "每小时实时监控", when: "发送时段内每小时" },
  };
  const OUTBOX = {
    DELIVERED: ["已送达", "ok"], PENDING: ["等待发送", "info"], RETRY: ["等待重试", "warn"],
    PRE_SEND_RETRY: ["等待重试", "warn"], PREPARING: ["发送中", "info"], SUBMITTING: ["发送中", "info"],
    SENDING: ["发送中", "info"], DELIVERY_UNKNOWN: ["可能已提交（不自动重发）", "warn"], SKIPPED: ["已跳过", ""],
    EXPIRED: ["已过发送时段", ""], DEAD: ["发送失败", "bad"], REJECTED: ["飞书拒绝", "bad"],
  };
  const RUNS = { SUCCEEDED: ["成功", "ok"], FAILED: ["失败", "bad"], SKIPPED: ["跳过", "warn"] };
  const TOGGLES = { daily: ["enabled", "to_group", "mention_operators", "to_lead"],
                    review: ["enabled", "to_group", "mention_operators", "to_lead", "to_operators", "weekly"],
                    hourly: ["enabled", "to_group", "mention_operators", "to_lead", "to_operators"] };
  const NUMBERS = ["start_hour", "end_hour", "high_spend", "low_spend", "min_clicks", "acos_high", "cvr_low",
                   "acos_good", "min_orders_good"];
  const INTEGERS = ["start_hour", "end_hour", "min_clicks", "min_orders_good"];
  const MAX_OPERATORS = 10;
  const MAX_LEADS = 10;
  const SKILL_STATES = {
    on: ["使用中", "ok"], off: ["已停用", ""], missing_tools: ["需开通工具", "warn"],
    not_installed: ["未安装", "warn"], write_off: ["写入未开启", ""], unsupported: ["不支持，不安装", "bad"],
  };
  const WRITES = {
    RUNNING: ["已提交，结果未知", "warn"], PREVIEW: ["预览（待二次确认）", "info"], EXECUTED: ["已执行", "ok"],
    FAILED: ["失败", "bad"], BLOCKED: ["已拦截，未执行", ""],
  };

  // Leads are lists since multiple leads (older servers answered one object or null).
  function leadList(value) {
    return Array.isArray(value) ? value : value ? [value] : [];
  }

  function leadNames(value) {
    return leadList(value).map((person) => person.name).join("、");
  }

  function skillState(entry) {
    const found = SKILL_STATES[entry && entry.state] || [entry && entry.state ? entry.state : "—", ""];
    const tools = (entry && entry.tools) || [];
    const kind = entry && entry.state;
    const detail = kind === "missing_tools" ? "当前 SparkX Token 没有提供：" + tools.join("、")
      : kind === "unsupported" ? "需要本系统不提供的工具：" + tools.join("、")
        : kind === "write_off" ? "开启「广告写入」后启用"
          : kind === "not_installed" ? "点「安装 / 更新」后可用" : "";
    return { text: found[0], tone: found[1], detail };
  }

  function writeLabel(status) {
    const found = WRITES[status];
    return found ? { text: found[0], tone: found[1] } : { text: status || "—", tone: "" };
  }

  function fileSize(bytes) {
    const number = Number(bytes);
    if (!Number.isFinite(number) || number < 0) return "—";
    return number < 1024 ? `${number} B` : number < 1024 * 1024 ? `${(number / 1024).toFixed(1)} KB`
      : `${(number / 1024 / 1024).toFixed(1)} MB`;
  }

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

  function siteOf(row) {
    return row.country_label || "—";
  }

  function storeCounts(rows, site) {
    const list = (rows || []).filter((row) => !site || siteOf(row) === site);
    return { all: list.length, unmapped: list.filter((row) => row.monitored && !(row.operators || []).length).length,
             paused: list.filter((row) => !row.monitored).length };
  }

  function siteOptions(rows) {
    const counts = new Map();
    for (const row of rows || []) counts.set(siteOf(row), (counts.get(siteOf(row)) || 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-CN"))
      .map(([label, count]) => ({ label, count }));
  }

  function filterStores(rows, query, kind, site) {
    const needle = String(query || "").trim().toLowerCase();
    return (rows || []).filter((row) => {
      if (site && siteOf(row) !== site) return false;
      if (kind === "unmapped" && !(row.monitored && !(row.operators || []).length)) return false;
      if (kind === "paused" && row.monitored) return false;
      if (!needle) return true;
      return [row.name, row.short_name, row.country_label, row.profile_id, ...(row.operators || []).map((person) => person.name)]
        .some((value) => String(value || "").toLowerCase().includes(needle));
    });
  }

  function formatTokens(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "";
    return number >= 10000 ? `${(number / 10000).toFixed(1)} 万` : String(Math.round(number));
  }

  function durationText(seconds) {
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }

  // What a lane's preview button area says about its latest preview job.
  function previewStateText(entry, now) {
    if (!entry || !entry.status) return { text: "", tone: "" };
    if (entry.status === "running") {
      const started = new Date(entry.started_at).getTime();
      const seconds = Number.isFinite(started) ? Math.max(0, Math.round(((now === undefined ? Date.now() : now) - started) / 1000)) : null;
      return { text: "预览生成中" + (seconds === null ? "…" : `（已用 ${durationText(seconds)}）`) + "，点「预览」查看进度", tone: "running" };
    }
    if (entry.status === "failed") return { text: "上次预览失败，可重新预览", tone: "failed" };
    return { text: `已生成预览 · ${formatTime(entry.finished_at)}（30 分钟内重复打开不会重新生成）`, tone: "" };
  }

  function mentionText(mentions) {
    return (mentions || []).map((person) => `${person.name}（${(person.stores || []).join("、")}）`).join("、");
  }

  function promptValue(text, fallback) {
    const value = String(text || "").trim();
    return value === String(fallback || "").trim() ? "" : value;
  }

  function lanePayload(values, defaults) {
    const lanes = {};
    for (const [lane, keys] of Object.entries(TOGGLES)) {
      lanes[lane] = {};
      for (const key of keys) lanes[lane][key] = Boolean(values[lane] && values[lane][key]);
      lanes[lane].prompt = promptValue(values[lane] && values[lane].prompt, defaults && defaults[lane]);
      if (lanes[lane].prompt.length > 4000) throw new Error("任务说明不能超过 4000 字。");
    }
    for (const key of NUMBERS) {
      const raw = values.hourly ? values.hourly[key] : undefined;
      const number = Number(raw);
      if (raw === "" || raw === undefined || raw === null || !Number.isFinite(number)) throw new Error("请填写完整的监控阈值。");
      lanes.hourly[key] = INTEGERS.includes(key) ? Math.trunc(number) : number;
    }
    if (lanes.hourly.start_hour >= lanes.hourly.end_hour) throw new Error("发送时段的开始须早于结束。");
    if (lanes.hourly.low_spend >= lanes.hourly.high_spend) throw new Error("低花费起算须小于高花费线。");
    return lanes;
  }

  function setupSteps(overview) {
    const data = overview || {};
    const sparkx = data.sparkx || {}, deepseek = data.deepseek || {}, feishu = data.feishu || {};
    const skills = data.skills || {}, stores = data.stores || {};
    return [
      { key: "sparkx", title: "保存 SparkX MCP Token", detail: "DeepSeek 通过它读取广告数据（只读）", done: Boolean(sparkx.configured), view: "sparkx" },
      { key: "skills", title: "安装 SparkX 官方 Skills", detail: "让 DeepSeek 按官方方法查询和分析", done: Boolean(skills.installed), view: "sparkx" },
      { key: "deepseek", title: "配置 DeepSeek", detail: "三条通知和飞书问答都由 DeepSeek 完成", done: Boolean(deepseek.configured), view: "deepseek" },
      { key: "recipients", title: "确认通知群和负责人", detail: "通知群、总负责人、广告负责人",
        done: Boolean(feishu.group && feishu.company_lead && feishu.ad_lead), view: "notifications" },
      { key: "operators", title: "为店铺指定运营（可选）", detail: "指定后群消息会 @ 出问题店铺的运营",
        done: Boolean(stores.mapped), view: "stores", optional: true },
      { key: "delivery", title: "开启飞书发送", detail: "服务器配置 FEISHU_ENABLED、APP_DRY_RUN、SCHEDULER_ENABLED",
        done: Boolean(feishu.delivery_enabled && feishu.scheduler_enabled), view: null },
    ];
  }

  function start(doc, win) {
    if (doc.documentElement.dataset.jxStarted) return;  // One set of listeners per page.
    doc.documentElement.dataset.jxStarted = "1";
    const $ = (id) => doc.getElementById(id);
    const csrf = doc.querySelector('meta[name="jx-console-csrf"]').content;
    const state = { view: "overview", overview: null, stores: null, filter: "all", site: "", selected: new Set(), members: null,
                    dialog: null, reports: null, saved: null, recipients: null, groups: null, sparkx: null,
                    deepseek: null, skills: null };
    // Latest preview job per lane (kept while the drawer is closed) and the lane shown in the drawer.
    const previews = {};
    const drawer = { lane: null, rendered: null, attention: false };

    // ---------------------------------------------------------------- helpers
    function el(tag, text, className) {
      const node = doc.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (className) node.className = className;
      return node;
    }

    function badge(label) {
      return el("span", label.text, "badge" + (label.tone ? " " + label.tone : ""));
    }

    function setBadge(node, label) {
      node.textContent = label.text;
      node.className = "badge" + (label.tone ? " " + label.tone : "");
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

    function toast(text, tone) {
      const node = el("div", text, "toast" + (tone ? " " + tone : ""));
      node.setAttribute("role", tone === "error" ? "alert" : "status");
      $("toasts").append(node);
      win.setTimeout(() => node.remove(), tone === "error" ? 7000 : 3500);
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
      if (!response.ok) {
        const error = new Error((data && data.error) || "请求失败，请稍后重试。");
        error.status = response.status;
        throw error;
      }
      return data;
    }

    async function guarded(action, busy) {
      if (busy) { busy.disabled = true; busy.setAttribute("aria-busy", "true"); }
      try { return await action(); }
      catch (error) { toast(error.message, "error"); return null; }
      finally { if (busy) { busy.disabled = false; busy.removeAttribute("aria-busy"); } }
    }

    // ---------------------------------------------------------------- dialogs
    function syncScrollLock() {
      // While a dialog or the drawer is open the page behind it must not scroll.
      const open = [...doc.querySelectorAll(".overlay")].some((overlay) => !overlay.hidden);
      doc.documentElement.classList.toggle("scroll-locked", open);
    }

    function openOverlay(id, focus) {
      const overlay = $(id);
      overlay.hidden = false;
      syncScrollLock();
      const target = focus || overlay.querySelector("input, select, textarea, button");
      if (target && target.focus) target.focus();
    }

    function closeOverlay(id) {
      $(id).hidden = true;
      if (id === "preview-drawer") drawer.lane = null;  // the job keeps running; the lane shows its state
      syncScrollLock();
    }

    function scrollToTop() {
      const scroller = doc.scrollingElement || doc.documentElement;
      if (scroller) scroller.scrollTop = 0;
    }

    for (const overlay of doc.querySelectorAll(".overlay")) {
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay || event.target.closest("[data-close]")) closeOverlay(overlay.id);
      });
    }
    doc.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      for (const overlay of doc.querySelectorAll(".overlay")) if (!overlay.hidden) closeOverlay(overlay.id);
    });

    // ---------------------------------------------------------------- overview
    function stat(title, value, detail, tone, view) {
      const node = el("article", null, "stat");
      const head = el("div", null, "stat-head");
      head.append(el("h2", title));
      if (tone) head.append(badge(tone));
      node.append(head, el("strong", value), ...[].concat(detail).filter(Boolean).map((line) => el("span", line, "detail")));
      if (view) {
        const link = el("button", "设置", "button ghost small link");
        link.type = "button";
        link.addEventListener("click", () => show(view));
        node.append(link);
      }
      return node;
    }

    function renderSetup(data) {
      const steps = setupSteps(data);
      const required = steps.filter((step) => !step.optional);
      const done = required.filter((step) => step.done).length;
      $("setup-panel").hidden = done === required.length;
      setBadge($("setup-progress"), { text: `已完成 ${done}/${required.length}`, tone: done === required.length ? "ok" : "info" });
      $("setup-steps").replaceChildren(...steps.map((step, index) => {
        const item = el("li", null, "step" + (step.done ? " done" : ""));
        const text = doc.createElement("div");
        text.append(el("strong", step.title), el("span", step.detail, "muted"));
        let action;
        if (step.done) action = badge({ text: "已完成", tone: "ok" });
        else if (step.view) {
          action = el("button", "去设置", "button secondary small");
          action.type = "button";
          action.addEventListener("click", () => show(step.view));
        } else action = badge({ text: "需服务器配置", tone: "warn" });
        item.append(el("span", step.done ? "✓" : String(index + 1), "step-mark"), text, action);
        return item;
      }));
    }

    function laneResult(last) {
      if (!last) return "—";
      const parts = [runLabel(last.status).text];
      if (last.queued) parts.push(`排队 ${last.queued} 条`);
      if (last.findings !== undefined && last.findings !== null) parts.push(`提醒 ${last.findings} 个`);
      if (last.sparkx_calls) parts.push(`查询 SparkX ${last.sparkx_calls} 次`);
      if (last.tokens) parts.push(`Token ${formatTokens(last.tokens)}` + (last.model ? `（${last.model}）` : ""));
      if (last.message) parts.push(last.message);
      return parts.join("；");
    }

    async function loadOverview() {
      const data = await guarded(() => api("/console/api/overview"));
      if (!data) return;
      state.overview = data;
      const sparkx = data.sparkx || {}, deepseek = data.deepseek || {}, feishu = data.feishu || {}, stores = data.stores || {};
      const skills = data.skills || {};
      renderSetup(data);
      $("status-cards").replaceChildren(
        stat("SparkX MCP", sparkx.configured ? "已连接" : "未配置",
          skills.installed ? `官方 Skills ${skills.release || ""}（${skills.count} 个）` : "官方 Skills 未安装",
          !sparkx.configured ? { text: "待配置", tone: "warn" }
            : (data.writes || {}).enabled ? { text: "广告写入已开启", tone: "warn" } : { text: "只读", tone: "ok" }, "sparkx"),
        stat("DeepSeek", deepseek.configured ? (deepseek.model || "已配置") : "未配置",
          deepseek.configured ? (deepseek.thinking === false ? "深度思考已关闭" : "深度思考已开启") : "未配置时三条通知不会运行",
          deepseek.configured ? { text: "可用", tone: "ok" } : { text: "待配置", tone: "warn" }, "deepseek"),
        stat("飞书发送", feishu.delivery_enabled ? "已开启" : (feishu.dry_run ? "演练模式" : "未开启"),
          "通知群：" + (feishu.group || "未设置") + (feishu.bot_enabled ? " · 机器人已开启" : " · 机器人未开启"),
          feishu.delivery_enabled ? { text: "正常", tone: "ok" } : { text: "不发送", tone: "warn" }, "notifications"),
        stat("店铺", `${stores.monitored || 0} / ${stores.authorized || 0}`,
          [`纳入通知 / Token 授权；${stores.mapped || 0} 家已指定运营；更新于 ${formatTime(stores.roster_at)}`,
           Object.keys(stores.sites || {}).length ? "纳入通知：" + Object.entries(stores.sites)
             .sort((a, b) => b[1] - a[1]).map(([site, count]) => `${site} ${count}`).join(" · ") : ""], null, "stores"));
      const lanes = data.lanes || {};
      fill("lane-rows", Object.entries(LANES).map(([key, meta]) => {
        const lane = lanes[key] || {}, last = lane.last || null;
        const when = key === "hourly" && lane.start_hour !== undefined ? `每小时 ${lane.start_hour}:05–${lane.end_hour}:00` : meta.when;
        return row([meta.label, when, badge(lane.enabled ? { text: "开启", tone: "ok" } : { text: "关闭", tone: "" }),
          last ? formatTime(last.at) : "—", laneResult(last)]);
      }), "", 5);
      fill("recent-rows", (data.recent || []).map((item) => row([formatTime(item.created_at), item.lane, item.recipient,
        badge(statusLabel(item.status))])), "还没有发送记录。", 4);
    }

    // ---------------------------------------------------------------- stores
    function chips(people, emptyText) {
      const span = el("span", null, "chips");
      if (!people || !people.length) { span.append(el("span", emptyText, "chip empty")); return span; }
      for (const person of people) span.append(el("span", person.name || person.id, "chip"));
      return span;
    }

    function visibleStores() {
      return state.stores ? filterStores(state.stores.rows, $("store-search").value, state.filter, state.site) : [];
    }

    function renderSites(rows) {
      const options = siteOptions(rows);
      if (state.site && !options.some((option) => option.label === state.site)) state.site = "";
      const all = el("option", `全部站点（${rows.length}）`);
      all.value = "";
      $("store-site").replaceChildren(all, ...options.map((option) => {
        const node = el("option", `${option.label}（${option.count}）`);
        node.value = option.label;
        return node;
      }));
      $("store-site").value = state.site;
    }

    function renderBulk() {
      const count = state.selected.size;
      $("bulk-bar").hidden = count === 0;
      $("bulk-count").textContent = `已选 ${count} 家店铺`;
      const visible = visibleStores();
      const all = $("select-all");
      const chosen = visible.filter((store) => state.selected.has(store.profile_id)).length;
      all.checked = visible.length > 0 && chosen === visible.length;
      all.indeterminate = chosen > 0 && chosen < visible.length;
    }

    function renderStores() {
      const data = state.stores;
      if (!data) return;
      $("roster-time").textContent = "店铺列表更新于 " + formatTime(data.roster_at);
      renderSites(data.rows);
      const counts = storeCounts(data.rows, state.site);
      for (const span of doc.querySelectorAll("[data-count]")) span.textContent = String(counts[span.dataset.count]);
      for (const button of doc.querySelectorAll("[data-filter]")) button.setAttribute("aria-pressed", String(button.dataset.filter === state.filter));
      const rows = visibleStores().map((store) => {
        const check = doc.createElement("input");
        check.type = "checkbox";
        check.checked = state.selected.has(store.profile_id);
        check.setAttribute("aria-label", "选择 " + store.short_name);
        check.addEventListener("change", () => {
          if (check.checked) state.selected.add(store.profile_id); else state.selected.delete(store.profile_id);
          check.closest("tr").classList.toggle("selected", check.checked);
          renderBulk();
        });
        const name = el("div", null, "store-name");
        name.append(el("strong", store.short_name), el("span", store.name + (store.authorized ? "" : "（Token 已不再授权）")));
        const toggle = el("label", null, "switch");
        const input = doc.createElement("input");
        input.type = "checkbox"; input.setAttribute("role", "switch"); input.checked = store.monitored;
        input.setAttribute("aria-label", "纳入通知 " + store.short_name);
        input.addEventListener("change", () => saveStores({ [store.profile_id]: { monitored: input.checked } }, input,
          input.checked ? `${store.short_name} 已纳入通知。` : `${store.short_name} 已暂停通知。`));
        toggle.append(input, el("span", null, "switch-track"));
        const edit = el("button", "编辑运营", "button ghost small");
        edit.type = "button";
        edit.addEventListener("click", () => openMembers({ mode: "operators", stores: [store] }));
        const tr = row([check, name, store.country_label, store.store_type === "vendor" ? "Vendor" : store.store_type === "seller" ? "Seller" : "—",
          chips(store.operators, "未映射运营"), toggle, edit]);
        tr.cells[0].className = "col-check";
        tr.cells[6].className = "col-actions";
        if (check.checked) tr.classList.add("selected");
        return tr;
      });
      fill("store-rows", rows, data.rows.length ? "没有符合条件的店铺。" : "还没有店铺：请先在「SparkX MCP」保存 Token，再点「从 SparkX 刷新店铺」。", 7);
      renderBulk();
    }

    async function loadStores() {
      const data = await guarded(() => api("/console/api/stores"));
      if (data) {
        state.stores = data;
        const known = new Set(data.rows.map((store) => store.profile_id));
        for (const key of [...state.selected]) if (!known.has(key)) state.selected.delete(key);
        renderStores();
      }
    }

    async function saveStores(changes, control, message) {
      const rows = new Map((state.stores.rows || []).map((store) => [store.profile_id, store]));
      const body = { revision: state.stores.revision, changes: {} };
      for (const [key, change] of Object.entries(changes)) {
        const store = rows.get(key);
        if (!store) continue;
        body.changes[key] = { monitored: change.monitored === undefined ? store.monitored : change.monitored,
                              operator_ids: change.operator_ids || store.operators.map((person) => person.id) };
      }
      const data = await guarded(() => api("/console/api/stores/save", { method: "POST", body }), control);
      if (data) { state.stores = data; renderStores(); toast(message || "已保存。"); }
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

    function renderMembers() {
      const dialog = state.dialog;
      const container = $("member-list");
      const needle = $("member-search").value.trim().toLowerCase();
      const items = (state.members || []).filter((person) => !needle || person.name.toLowerCase().includes(needle));
      const legend = container.querySelector("legend");
      container.replaceChildren(...(legend ? [legend] : []), ...items.map((person) => {
        const label = el("label", null, "check");
        const input = doc.createElement("input");
        input.type = "checkbox";
        input.name = "member"; input.value = person.id; input.checked = dialog.selected.has(person.id);
        input.addEventListener("change", () => {
          if (input.checked) dialog.selected.add(person.id); else dialog.selected.delete(person.id);
          renderMemberHint();
        });
        label.append(input, doc.createTextNode(person.name));
        return label;
      }));
      if (!items.length) container.append(el("p", "没有匹配的群成员。", "muted"));
      renderMemberHint();
    }

    function renderMemberHint() {
      const dialog = state.dialog;
      const count = dialog.selected.size;
      $("member-hint").textContent = dialog.mode === "lead" ? `已选 ${count} 位（最多 ${MAX_LEADS} 位），每人都私聊接收。`
        : `已选 ${count} 位（每家店最多 ${MAX_OPERATORS} 位）` + (dialog.stores.length > 1 ? "；保存后替换所选店铺的运营。" : "");
    }

    async function openMembers(options) {
      try { await members(options.mode === "lead"); } catch (error) { toast(error.message, "error"); return; }
      const initial = options.mode === "lead" ? leadList(options.current).map((person) => person.id)
        : options.stores.length === 1 ? options.stores[0].operators.map((person) => person.id) : [];
      state.dialog = { ...options, selected: new Set(initial) };
      $("member-dialog-title").textContent = options.mode === "lead" ? (options.kind === "company" ? "选择总负责人" : "选择广告负责人") : "指定运营";
      $("member-dialog-subtitle").textContent = options.mode === "lead" ? "从通知群成员中选择，可多选，私聊接收。"
        : options.stores.length === 1 ? `${options.stores[0].short_name} · ${options.stores[0].country_label}` : `批量设置 ${options.stores.length} 家店铺`;
      $("member-clear").hidden = options.mode !== "lead";
      $("member-search").value = "";
      renderMembers();
      openOverlay("member-dialog", $("member-search"));
    }

    // ---------------------------------------------------------------- notifications
    function renderRecipients() {
      const data = state.recipients;
      $("current-group").textContent = (data.group_detail && data.group_detail.name) || data.group || "未设置";
      for (const kind of ["company", "ad"]) {
        const names = leadNames(data[kind]);
        const chip = $("lead-" + kind);
        chip.textContent = names || "未设置";
        chip.className = "chip" + (names ? "" : " empty");
      }
    }

    function defaultPrompt(lane) {
      return ((state.reports && state.reports.default_prompts) || {})[lane] || "";
    }

    function renderPromptState(lane) {
      const area = doc.querySelector(`#lanes-form [data-lane="${lane}"] textarea[name="prompt"]`);
      const custom = promptValue(area.value, defaultPrompt(lane));
      doc.querySelector(`[data-prompt-state="${lane}"]`).textContent = custom ? "已自定义" : (defaultPrompt(lane) ? "使用默认说明" : "未填写");
    }

    function renderLanes() {
      const lanes = state.reports.lanes;
      for (const section of doc.querySelectorAll("#lanes-form [data-lane]")) {
        const lane = section.dataset.lane;
        for (const input of section.querySelectorAll("input, textarea")) {
          if (input.type === "checkbox") input.checked = Boolean(lanes[lane][input.name]);
          else if (input.name === "prompt") input.value = lanes[lane].prompt || defaultPrompt(lane);
          else input.value = lanes[lane][input.name];
        }
        section.classList.toggle("off", !lanes[lane].enabled);
        renderPromptState(lane);
      }
      state.saved = JSON.stringify(formValues());
      $("lanes-updated").textContent = state.reports.updated_at ? "上次保存：" + formatTime(state.reports.updated_at) : "当前为默认设置";
      renderDirty();
    }

    function formValues() {
      const values = {};
      for (const section of doc.querySelectorAll("#lanes-form [data-lane]")) {
        const lane = section.dataset.lane;
        values[lane] = {};
        for (const input of section.querySelectorAll("input, textarea")) {
          values[lane][input.name] = input.type === "checkbox" ? input.checked : input.value;
        }
      }
      return values;
    }

    function renderDirty() {
      $("lanes-savebar").hidden = !state.reports || JSON.stringify(formValues()) === state.saved;
    }

    async function loadNotifications() {
      const [recipients, reports] = await Promise.all([
        guarded(() => api("/console/api/recipients")), guarded(() => api("/console/api/reports"))]);
      if (recipients) { state.recipients = recipients; renderRecipients(); }
      if (reports) { state.reports = reports; renderLanes(); }
      // Previews started earlier (also before a page reload) are still on the server for 30 minutes.
      for (const lane of Object.keys(LANES)) {
        if (previews[lane]) { renderPreviewState(lane); continue; }
        api("/console/api/reports/preview?lane=" + lane).then((data) => { if (data && data.job) trackPreview(lane, data); })
          .catch(() => {});
      }
    }

    function renderCard(value) {
      const container = el("article", null, "feishu-card");
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
      return container;
    }

    function previewInfo(info) {
      const usage = info.usage || {};
      return [info.report_date ? "数据日 " + info.report_date : "", info.window ? "近 7 日 " + info.window.join(" ~ ") : "",
        info.day ? "美西 " + info.day : "", info.profiles !== undefined ? `店铺 ${info.profiles} 家` : "",
        info.sparkx_calls !== undefined && info.sparkx_calls !== null ? `SparkX 查询 ${info.sparkx_calls} 次` : "",
        info.findings !== undefined ? `提醒 ${info.findings} 个` : "", info.seconds !== undefined ? `用时 ${info.seconds} 秒` : "",
        usage.total_tokens ? `Token ${formatTokens(usage.total_tokens)}` + (usage.prompt_cache_hit_tokens
          ? `（缓存命中 ${formatTokens(usage.prompt_cache_hit_tokens)}）` : "") : "",
        info.model || ""].filter(Boolean).join(" · ");
    }

    // Preview jobs live on the server. Asking again for a lane returns its running job, or the finished one
    // for the same settings, so closing the drawer by accident never costs a second DeepSeek run.
    function trackPreview(lane, data) {
      const entry = previews[lane] || (previews[lane] = { timer: null, failures: 0 });
      for (const key of ["job", "status", "result", "error", "started_at", "finished_at", "sent", "current", "reused"]) {
        if (data[key] !== undefined) entry[key] = data[key];
      }
      renderPreviewState(lane);
      if (drawer.lane === lane) renderDrawer();
      if (entry.status === "running") schedulePreviewPoll(lane);
    }

    function schedulePreviewPoll(lane) {
      const entry = previews[lane];
      if (!entry || entry.timer) return;
      entry.timer = win.setTimeout(async () => {
        entry.timer = null;
        const job = entry.job;
        let data;
        try {
          data = await api("/console/api/reports/preview?job=" + encodeURIComponent(job));
          entry.failures = 0;
        } catch (error) {
          if (entry.job !== job) return;
          if (error.status === 404 || ++entry.failures >= 5) trackPreview(lane, { status: "failed", error: error.message });
          else schedulePreviewPoll(lane);  // a passing network error: keep following the job
          return;
        }
        if (entry.job === job) trackPreview(lane, data);
      }, win.JX_PREVIEW_INTERVAL || 3000);
    }

    function renderPreviewState(lane) {
      const node = doc.querySelector(`[data-preview-state="${lane}"]`);
      if (!node) return;
      const label = previewStateText(previews[lane]);
      node.textContent = label.text;
      node.className = "preview-state" + (label.tone ? " " + label.tone : "");
    }

    function renderDrawer() {
      const lane = drawer.lane, entry = previews[lane];
      if (!lane || !entry) return;
      const running = entry.status === "running", done = entry.status === "done";
      const result = done ? (entry.result || {}) : null;
      $("preview-progress").hidden = !running;
      if (running) {
        const label = previewStateText(entry);
        $("preview-progress-text").textContent = "DeepSeek 正在调用 SparkX 查询并撰写…" +
          label.text.replace(/^预览生成中/, "").replace(/，点「预览」查看进度$/, "") + "，通常需要 1–10 分钟。可以先关闭，稍后再点「预览」回来查看。";
      }
      notice("preview-error", entry.status === "failed" ? (entry.error || "预览失败。") : "", "error");
      const stale = entry.current === false;
      notice("preview-notice", stale ? "正在显示的是修改设置之前开始生成的预览；" + (running ? "完成后" : "") + "点「重新生成」可按当前设置重新预览。"
        : done && entry.reused ? `这是 ${formatTime(entry.finished_at)} 生成的预览，没有重新调用 DeepSeek；需要最新数据请点「重新生成」。` : "",
        stale ? "warn" : "");
      const key = `${entry.job}:${entry.status}`;
      if (drawer.rendered !== key) {
        drawer.rendered = key;
        const cards = result ? result.cards || [] : [];
        $("preview-cards").replaceChildren(...(result ? (cards.length ? cards.map(renderCard) : [el("p", result.text || "没有内容。", "muted")]) : []));
        $("preview-text").textContent = result ? result.text || "" : "";
        $("preview-plain").hidden = !cards.length;
      }
      $("preview-info").textContent = result ? previewInfo(result.info || {}) : "只读：预览不会排队或发送消息。";
      $("preview-regenerate").disabled = running;
      renderTestSend(lane, entry, result);
    }

    function renderTestSend(lane, entry, result) {
      const cards = result ? (result.cards || []).length : 0;
      $("test-send").disabled = !cards;
      const kind = lane === "daily" ? "company" : "ad";
      const known = Boolean(state.recipients);
      const names = known ? leadNames(state.recipients[kind]) : "";
      $("test-lead-label").textContent = (lane === "daily" ? "总负责人" : "广告负责人") + (names ? `（${names}）` : known ? "（未设置）" : "");
      $("test-to-lead").disabled = known && !names;
      if (known && !names) $("test-to-lead").checked = false;
      const mentions = result && result.info ? result.info.mentions || [] : [];
      $("test-mention-label").textContent = mentions.length ? "@ 对应运营：" + mentionText(mentions) : "@ 对应运营（这份预览没有可 @ 的运营）";
      $("test-mention").disabled = !mentions.length;
      if (!mentions.length) $("test-mention").checked = false;
      const sent = (entry.sent || []).map((name) => (name === "group" ? "通知群" : lane === "daily" ? "总负责人" : "广告负责人"));
      $("test-send-hint").textContent = !result ? "预览生成后可以发送测试。" : !cards ? "这份预览没有内容，无法发送测试。"
        : sent.length ? `已发送测试到：${sent.join("、")}。同一份预览不会重复发给同一对象；需要新内容请「重新生成」。`
          : "发送的就是上面这份预览" + (result.info && result.info.excel ? "和它的 Excel 文件" : "") +
            "，不会再调用 DeepSeek，也不计入定时发送。";
      if (drawer.attention && cards) {
        drawer.attention = false;
        $("test-send").classList.remove("lane-attention");
        void $("test-send").offsetWidth;  // restart the highlight animation
        $("test-send").classList.add("lane-attention");
        $("test-send-button").focus();
      }
    }

    async function requestPreview(lane, options) {
      const { fresh = false, button = null, test = false } = options || {};
      let lanes;
      try { lanes = lanePayload(formValues(), state.reports.default_prompts); } catch (error) { toast(error.message, "error"); return; }
      const body = { lane, draft: lanes[lane] };
      if (fresh) body.fresh = true;
      const data = await guarded(() => api("/console/api/reports/preview", { method: "POST", body }), button);
      if (!data) return;
      drawer.lane = lane;
      drawer.attention = test;
      $("preview-title").textContent = "预览 · " + LANES[lane].label;
      if ($("preview-drawer").hidden) openOverlay("preview-drawer", doc.querySelector("#preview-drawer [data-close]"));
      trackPreview(lane, data);
      if (fresh && data.reused) toast("预览正在生成，完成后再重新生成。", "warn");
    }

    async function sendTest(button) {
      const lane = drawer.lane, entry = previews[lane];
      if (!entry || entry.status !== "done") return;
      const body = { job: entry.job, to_group: $("test-to-group").checked, to_lead: $("test-to-lead").checked && !$("test-to-lead").disabled,
                     mention: $("test-mention").checked && !$("test-mention").disabled };
      if (!body.to_group && !body.to_lead) { toast("请至少选择一个接收对象。", "warn"); return; }
      const data = await guarded(() => api("/console/api/reports/test-send", { method: "POST", body }), button);
      if (!data) return;
      trackPreview(lane, { sent: data.sent });
      if (data.status === "QUEUED") toast(`测试消息已排队（${data.cards === undefined ? data.queued : data.cards} 张卡片` +
        (data.files ? ` + ${data.files} 个 Excel` : "") + (data.mentions ? `，@ ${data.mentions} 位运营` : "") +
        "），约 10 秒内发出，可在「发送记录」查看结果。");
      else toast("这份预览已经发送过测试，没有重复发送。", "warn");
    }

    // ---------------------------------------------------------------- sparkx & deepseek
    function facts(id, entries) {
      const list = $(id);
      list.replaceChildren();
      for (const [term, value] of entries) list.append(el("dt", term), el("dd", value));
    }

    function renderSkills(data) {
      const installed = data.installed;
      setBadge($("skills-badge"), installed ? { text: "已安装", tone: "ok" } : { text: "未安装", tone: "warn" });
      const entries = [["来源", "GitHub " + (data.source || "SparkXAI/SparkXAI-MCP")]];
      if (installed) {
        entries.push(["版本", `${installed.release || installed.ref || "—"}` + (installed.commit ? `（${installed.commit.slice(0, 7)}）` : "")],
          ["安装时间", formatTime(installed.installed_at)]);
      }
      if (data.latest) {
        entries.push(["官方最新", `${data.latest.release || "—"}（${data.latest.updated_at || "—"}）` +
          (data.latest.outdated && data.latest.outdated.length ? `，可更新：${data.latest.outdated.join("、")}` : "，已是最新")]);
      }
      if (data.error) entries.push(["检查结果", data.error]);
      facts("skills-facts", entries);
      state.skills = data;
      const catalog = data.catalog && data.catalog.length ? data.catalog
        : (data.skills || []).map((skill) => ({ ...skill, state: "on", tools: [] }));
      fill("skills-rows", catalog.map((skill) => {
        const label = skillState(skill);
        const box = doc.createElement("input");
        box.type = "checkbox";
        box.dataset.skill = skill.name;
        // Write Skills follow the 「广告写入」 switch; only optional read Skills can be ticked here.
        const fixed = Boolean(skill.required) || Boolean(skill.write);
        box.checked = skill.state === "on" || skill.state === "missing_tools"
          || (Boolean(skill.required) && !skill.write && skill.state !== "unsupported" && skill.state !== "not_installed");
        box.disabled = fixed || skill.state === "unsupported" || skill.state === "not_installed";
        box.setAttribute("aria-label", "启用 " + skill.name);
        const name = el("div", null, "skill-name");
        name.append(el("strong", skill.name), el("span", skill.write ? "写入（随「广告写入」开关）"
          : skill.required ? "基础（始终启用）" : "选装", "muted"));
        const status = el("div", null, "skill-state");
        status.append(badge(label));
        if (label.detail) status.append(el("span", label.detail, "muted"));
        return row([box, name, skill.version || "—", status, (skill.mcp_tools || []).join("、"), skill.description || "—"]);
      }), "尚未安装：点「安装 / 更新」从 GitHub 获取官方 Skills。", 6);
      const used = catalog.filter((skill) => skill.state === "on").length;
      const present = catalog.filter((skill) => skill.state !== "unsupported" && skill.state !== "not_installed");
      const writes = present.filter((skill) => skill.write).length;
      const blocked = catalog.filter((skill) => skill.state === "unsupported").length;
      const waiting = catalog.filter((skill) => skill.state === "not_installed").length;
      $("skills-summary").textContent = catalog.length ? `已安装 ${present.length} 个（写入 ${writes} 个），使用中 ${used} 个` +
        (waiting ? `；${waiting} 个待安装` : "") + (blocked ? `；${blocked} 个需要本系统不提供的工具，未安装` : "") : "";
      $("tool-chips").replaceChildren(...(data.read_tools || []).map((name) => el("span", name, "chip mono")));
    }

    function renderWrites(data) {
      state.writes = data;
      setBadge($("writes-badge"), data.enabled ? { text: "已开启", tone: "warn" } : { text: "已关闭（只读）", tone: "" });
      const offered = data.offered_write_tools;
      const entries = [
        ["状态", data.enabled ? "已开启" + (data.enabled_at ? `（${formatTime(data.enabled_at)} 起）` : "") : "已关闭：DeepSeek 只能查询"],
        ["可以修改的人", data.leads && data.leads.length ? data.leads.join("、") : "未设置负责人（在「通知与报告 → 负责人」选择）"],
        ["确认方式", `机器人列出修改计划后，同一位负责人在 30 分钟内回复「${data.confirm_word || "确认执行"}」`],
        ["本 Token 的写入工具", offered === null || offered === undefined ? "暂时无法向 SparkX 查询"
          : offered.length ? offered.join("、") : "SparkX 没有为这个 Token 提供写入工具"],
        ["单次上限", `每次确认最多 ${data.max_writes || 30} 次写入调用`]];
      if (data.error) entries.push(["读取结果", data.error]);
      facts("writes-facts", entries);
      $("writes-enabled").checked = Boolean(data.enabled);
      $("writes-confirm").checked = false;
      $("writes-confirm-row").hidden = true;
      fill("writes-rows", (data.log || []).map((item) => row([formatTime(item.at), item.requester || "—", item.tool,
        badge(writeLabel(item.status)), el("span", item.request || "—", "payload"), el("span", item.result || "—", "payload")])),
        "还没有写入记录。", 6);
    }

    async function loadWrites() {
      const data = await guarded(() => api("/console/api/sparkx/write"));
      if (data) renderWrites(data);
    }

    async function saveSkillChoice(box) {
      const data = state.skills;
      if (!data || !data.choices) return;
      const disabled = [...doc.querySelectorAll("#skills-rows input[data-skill]")]
        .filter((input) => !input.disabled && !input.checked).map((input) => input.dataset.skill);
      const saved = await guarded(() => api("/console/api/skills/choices", {
        method: "POST", body: { revision: data.choices.revision, disabled } }), box);
      if (saved) { renderSkills(saved); toast(`${box.checked ? "已启用" : "已停用"} ${box.dataset.skill}，下次运行生效。`); }
      else renderSkills(data);
    }

    async function loadSparkx() {
      const [data, skills] = await Promise.all([guarded(() => api("/console/api/sparkx")), guarded(() => api("/console/api/skills")),
        loadWrites()]);
      if (data) {
        state.sparkx = data;
        setBadge($("sparkx-badge"), data.configured ? { text: "已配置", tone: "ok" } : { text: "未配置", tone: "warn" });
        facts("sparkx-facts", [["接口", data.endpoint], ["Token", data.configured ? (data.token_source === "console" ? "已保存（管理台）" : "来自环境变量") : "未配置"],
          ["更新时间", formatTime(data.updated_at)]]);
      }
      if (skills) renderSkills(skills);
    }

    async function loadDeepseek() {
      const [data, overview] = await Promise.all([guarded(() => api("/console/api/deepseek")),
        state.overview ? Promise.resolve(state.overview) : guarded(() => api("/console/api/overview"))]);
      if (overview) {
        state.overview = overview;
        setBadge($("bot-badge"), overview.feishu && overview.feishu.bot_enabled ? { text: "已开启", tone: "ok" } : { text: "未开启", tone: "warn" });
      }
      if (!data) return;
      state.deepseek = data;
      setBadge($("deepseek-badge"), data.configured ? { text: "可用", tone: "ok" } : { text: "未配置", tone: "warn" });
      facts("deepseek-facts", [["状态", data.configured ? "已配置" : "未配置"],
        ["密钥来源", data.key_source === "console" ? "管理台" : data.key_source === "environment" ? "环境变量" : data.key_source ? "私有文件" : "—"],
        ["更新时间", formatTime(data.updated_at)]]);
      $("deepseek-model").replaceChildren(...(data.models || []).map((model) => {
        const option = el("option", model); option.value = model; option.selected = model === data.model; return option;
      }));
      $("deepseek-timeout").value = data.timeout || 120;
      $("deepseek-thinking").checked = data.thinking !== false;
    }

    async function loadDeliveries() {
      const [data, files] = await Promise.all([guarded(() => api("/console/api/deliveries")),
        guarded(() => api("/console/api/archive"))]);
      if (data) {
        fill("delivery-rows", data.rows.map((item) => row([formatTime(item.created_at), item.lane, item.recipient, item.title,
          badge(statusLabel(item.status)), item.error || (item.delivered_at ? "送达 " + formatTime(item.delivered_at) : "—")])),
          "还没有发送记录。", 6);
      }
      if (files) {
        fill("archive-rows", files.rows.map((item) => {
          const link = el("a", "下载", "button ghost small");
          link.href = "/console/api/archive/file?path=" + encodeURIComponent(item.path);
          link.setAttribute("download", item.name);
          return row([formatTime(item.saved_at), item.lane_label || item.lane, item.name, fileSize(item.size), link]);
        }), "还没有保存的 Excel 文件。", 5);
      }
    }

    // ---------------------------------------------------------------- navigation
    const loaders = { overview: loadOverview, stores: loadStores, notifications: loadNotifications, sparkx: loadSparkx,
                      deepseek: loadDeepseek, deliveries: loadDeliveries };

    function show(view) {
      if (!loaders[view]) return;
      if (state.view !== view) scrollToTop();  // a new page starts at its top, not at the old scroll position
      state.view = view;
      for (const button of doc.querySelectorAll("[data-view]")) {
        if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
      }
      let active = null;
      for (const panel of doc.querySelectorAll("[data-panel]")) {
        panel.hidden = panel.dataset.panel !== view;
        if (!panel.hidden) active = panel;
      }
      $("page-title").textContent = active.dataset.title || "";
      $("page-subtitle").textContent = active.dataset.subtitle || "";
      for (const button of doc.querySelectorAll("#page-actions [data-for]")) button.hidden = button.dataset.for !== view;
      notice("global-status", "");
      loaders[view]();
    }

    for (const button of doc.querySelectorAll("[data-view]")) button.addEventListener("click", () => show(button.dataset.view));
    for (const button of doc.querySelectorAll("[data-go]")) button.addEventListener("click", () => show(button.dataset.go));
    for (const button of doc.querySelectorAll("[data-refresh]")) button.addEventListener("click", () => loaders[button.dataset.refresh]());
    $("logout").addEventListener("click", async () => {
      try { await win.fetch("/console/logout", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } }); }
      finally { win.location.assign("/console/login"); }
    });

    // stores
    $("store-search").addEventListener("input", renderStores);
    $("store-site").addEventListener("change", (event) => { state.site = event.currentTarget.value; renderStores(); });
    for (const button of doc.querySelectorAll("[data-filter]")) {
      button.addEventListener("click", () => { state.filter = button.dataset.filter; renderStores(); });
    }
    $("select-all").addEventListener("change", (event) => {
      for (const store of visibleStores()) {
        if (event.currentTarget.checked) state.selected.add(store.profile_id); else state.selected.delete(store.profile_id);
      }
      renderStores();
    });
    $("bulk-clear").addEventListener("click", () => { state.selected.clear(); renderStores(); });
    $("bulk-assign").addEventListener("click", () => {
      const stores = state.stores.rows.filter((store) => state.selected.has(store.profile_id));
      if (stores.length) openMembers({ mode: "operators", stores });
    });
    for (const [id, monitored] of [["bulk-include", true], ["bulk-exclude", false]]) {
      $(id).addEventListener("click", (event) => {
        const changes = {};
        for (const key of state.selected) changes[key] = { monitored };
        saveStores(changes, event.currentTarget, `${state.selected.size} 家店铺已${monitored ? "纳入" : "暂停"}通知。`);
      });
    }
    $("refresh-roster").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/stores/refresh", { method: "POST" }), event.currentTarget);
      if (data) { state.stores = data; renderStores(); toast(`已从 SparkX 更新：${data.rows.length} 家店铺。`); }
    });

    // member dialog (operators and leads)
    $("member-search").addEventListener("input", renderMembers);
    $("member-save").addEventListener("click", async (event) => {
      const dialog = state.dialog;
      if (!dialog) return;
      const ids = [...dialog.selected];
      if (dialog.mode === "lead") {
        if (ids.length > MAX_LEADS) { toast(`每类负责人最多 ${MAX_LEADS} 位。`, "warn"); return; }
        const data = await guarded(() => api("/console/api/recipients/" + dialog.kind, {
          method: "POST", body: { open_ids: ids, revision: state.recipients.revision } }), event.currentTarget);
        if (data) { closeOverlay("member-dialog"); toast(ids.length > 1 ? `已保存 ${ids.length} 位负责人。` : "负责人已保存。"); loadNotifications(); }
        return;
      }
      if (ids.length > MAX_OPERATORS) { toast(`每家店最多 ${MAX_OPERATORS} 位运营。`, "warn"); return; }
      const changes = {};
      for (const store of dialog.stores) changes[store.profile_id] = { operator_ids: ids };
      const data = await saveStores(changes, event.currentTarget,
        dialog.stores.length > 1 ? `已为 ${dialog.stores.length} 家店铺指定运营。` : `已保存 ${dialog.stores[0].short_name} 的运营。`);
      if (data) { closeOverlay("member-dialog"); if (dialog.stores.length > 1) { state.selected.clear(); renderStores(); } }
    });
    $("member-clear").addEventListener("click", async (event) => {
      const dialog = state.dialog;
      if (!dialog || dialog.mode !== "lead") return;
      const data = await guarded(() => api("/console/api/recipients/" + dialog.kind, {
        method: "POST", body: { open_ids: [], revision: state.recipients.revision } }), event.currentTarget);
      if (data) { closeOverlay("member-dialog"); toast("已清空负责人。"); loadNotifications(); }
    });
    for (const button of doc.querySelectorAll("[data-lead]")) {
      button.addEventListener("click", () => openMembers({ mode: "lead", kind: button.dataset.lead, stores: [],
        current: state.recipients && state.recipients[button.dataset.lead] }));
    }

    // notifications
    $("load-groups").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/groups"), event.currentTarget);
      if (!data) return;
      state.groups = data;
      $("group-select").replaceChildren(...data.chats.map((chat) => {
        const option = el("option", chat.name); option.value = chat.id; option.selected = chat.id === data.chat_id; return option;
      }));
      $("group-confirm").checked = false;
      $("group-change").hidden = false;
    });
    $("group-cancel").addEventListener("click", () => { $("group-change").hidden = true; });
    $("save-group").addEventListener("click", async (event) => {
      if (!$("group-confirm").checked) { toast("请先勾选确认更换范围。", "warn"); return; }
      const body = { chat_id: $("group-select").value, revision: state.groups.revision, confirmed: true };
      const data = await guarded(() => api("/console/api/groups", { method: "POST", body }), event.currentTarget);
      if (data) { $("group-change").hidden = true; toast("通知群已保存：" + data.name); state.members = null; loadNotifications(); }
    });
    $("lanes-form").addEventListener("input", (event) => {
      const section = event.target.closest("[data-lane]");
      if (section && event.target.name === "prompt") renderPromptState(section.dataset.lane);
      renderDirty();
    });
    $("lanes-form").addEventListener("change", (event) => {
      const section = event.target.closest("[data-lane]");
      if (section && event.target.name === "enabled") section.classList.toggle("off", !event.target.checked);
      renderDirty();
    });
    for (const button of doc.querySelectorAll("[data-reset-prompt]")) {
      button.addEventListener("click", () => {
        const lane = button.dataset.resetPrompt;
        doc.querySelector(`#lanes-form [data-lane="${lane}"] textarea[name="prompt"]`).value = defaultPrompt(lane);
        renderPromptState(lane);
        renderDirty();
      });
    }
    $("lanes-revert").addEventListener("click", () => renderLanes());
    $("lanes-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      let lanes;
      try { lanes = lanePayload(formValues(), state.reports.default_prompts); } catch (error) { toast(error.message, "error"); return; }
      const data = await guarded(() => api("/console/api/reports", {
        method: "POST", body: { revision: state.reports.revision, lanes } }), $("save-lanes"));
      if (data) { state.reports = data; renderLanes(); toast("通知设置已保存，下次运行生效。"); }
    });
    for (const button of doc.querySelectorAll("[data-preview]")) {
      button.addEventListener("click", () => requestPreview(button.dataset.preview, { button }));
    }
    for (const button of doc.querySelectorAll("[data-test]")) {
      button.addEventListener("click", () => requestPreview(button.dataset.test, { button, test: true }));
    }
    $("preview-regenerate").addEventListener("click", (event) => {
      if (drawer.lane) requestPreview(drawer.lane, { fresh: true, button: event.currentTarget });
    });
    $("test-send-button").addEventListener("click", (event) => sendTest(event.currentTarget));

    // sparkx
    $("sparkx-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = $("sparkx-token").value.trim();
      if (!token) { toast("请填写新的 Token。", "warn"); return; }
      const data = await guarded(() => api("/console/api/sparkx", { method: "POST", body: { revision: state.sparkx.revision, token } }),
        event.submitter || null);
      if (data) { $("sparkx-token").value = ""; toast("Token 已保存，可以点「测试连接」。"); loadSparkx(); }
    });
    $("sparkx-clear").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/sparkx", { method: "POST", body: { revision: state.sparkx.revision, clear: true } }),
        event.currentTarget);
      if (data) { toast("已清除管理台 Token（如环境变量中有 Token，将改用环境变量）。"); loadSparkx(); }
    });
    $("sparkx-test").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/sparkx/test", { method: "POST" }), event.currentTarget);
      if (!data) return;
      if (data.status !== "OK") { notice("sparkx-status", data.message || ("连接失败：" + data.status), "error"); return; }
      notice("sparkx-status", `连接成功：Token 授权 ${data.profile_count} 家店铺。`);
      fill("sparkx-profiles", data.profiles.map((item) => row([item.profile_id, item.name, item.country, item.store_type || "—"])),
        "Token 没有授权任何店铺。", 4);
      $("sparkx-profiles-panel").hidden = false;
    });
    $("skills-install").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/skills/install", { method: "POST" }), event.currentTarget);
      if (data) { renderSkills(data); toast("官方 Skills 已安装。"); }
    });
    $("skills-check").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/skills/check", { method: "POST" }), event.currentTarget);
      if (data) renderSkills(data);
    });
    $("skills-rows").addEventListener("change", (event) => {
      const box = event.target.closest("input[data-skill]");
      if (box) saveSkillChoice(box);
    });
    $("writes-enabled").addEventListener("change", () => {
      const turningOn = $("writes-enabled").checked && !(state.writes && state.writes.enabled);
      $("writes-confirm-row").hidden = !turningOn;
      if (!turningOn) $("writes-confirm").checked = false;
    });
    $("writes-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!state.writes) return;
      const enabled = $("writes-enabled").checked;
      if (enabled === Boolean(state.writes.enabled)) { toast("没有改动。", "warn"); return; }
      if (enabled && !$("writes-confirm").checked) { toast("开启前请先勾选确认。", "warn"); return; }
      const body = { enabled, revision: state.writes.revision, confirmed: enabled && $("writes-confirm").checked };
      const data = await guarded(() => api("/console/api/sparkx/write", { method: "POST", body }), event.submitter || null);
      if (!data) {
        $("writes-enabled").checked = Boolean(state.writes.enabled);
        $("writes-confirm-row").hidden = true;
        return;
      }
      renderWrites(data);
      state.overview = null;
      toast(enabled ? "广告写入已开启：负责人在飞书回复「确认执行」后才会修改。" : "广告写入已关闭，DeepSeek 只能查询。");
      const skills = await guarded(() => api("/console/api/skills"));
      if (skills) renderSkills(skills);
    });

    // deepseek
    $("deepseek-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = { revision: state.deepseek.revision, api_key: $("deepseek-key").value.trim(), model: $("deepseek-model").value,
                     timeout: Number($("deepseek-timeout").value), thinking: $("deepseek-thinking").checked };
      const data = await guarded(() => api("/console/api/deepseek", { method: "POST", body }), event.submitter || null);
      if (data) { $("deepseek-key").value = ""; toast("DeepSeek 设置已保存。"); state.overview = null; loadDeepseek(); }
    });
    $("deepseek-test").addEventListener("click", async (event) => {
      const data = await guarded(() => api("/console/api/deepseek/test", { method: "POST" }), event.currentTarget);
      if (!data) return;
      const messages = { OK: "连接成功，模型可用。", NOT_CONFIGURED: "尚未配置密钥。", INVALID_KEY: "密钥无效。",
        INSUFFICIENT_BALANCE: "账户余额不足。", MODEL_UNAVAILABLE: "当前模型不可用，请更换模型。", TIMEOUT: "连接超时。",
        UNREACHABLE: "无法连接 api.deepseek.com。" };
      notice("deepseek-status", messages[data.status] || ("连接失败：" + data.status), data.status === "OK" ? "" : "error");
    });

    show("overview");
  }

  return { start, statusLabel, runLabel, formatTime, parseBold, cardText, filterStores, storeCounts, siteOptions, promptValue,
           lanePayload, setupSteps, formatTokens, previewStateText, mentionText, leadList, leadNames, skillState, fileSize,
           writeLabel, LANES };
});
