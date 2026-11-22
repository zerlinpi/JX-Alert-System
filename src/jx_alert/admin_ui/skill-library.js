"use strict";

window.JxSkillLibrary = (() => {
  const mounts = new WeakMap();
  const node = (tag, text, className) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (className) n.className = className;
    return n;
  };
  const button = (text) => {
    const b = node("button", text, "button secondary");
    b.type = "button";
    return b;
  };
  const objectiveLabels = { balanced: "均衡", efficiency: "效率优先", reach: "曝光优先", discovery: "探索优先" };
  const auditLabels = { upload: "上传", enable: "设为可用", disable: "停用", rollback: "恢复版本" };

  function mount(root, { api, csrf }) {
    if (!root) return null;
    if (mounts.has(root)) return mounts.get(root);
    let catalog, selected, busy = false;
    const title = node("h2", "自定义 Skills 资料库");
    title.id = "skill-library-title";
    const intro = node("p", "把自己的 SKILL.md 保存到管理台，再审阅、恢复或导出版本。入库不会运行文件、调用模型或发送飞书通知。", "muted");
    const guide = node("details", undefined, "skill-format");
    guide.append(node("summary", "第一次上传？查看文件格式与示例"),
      node("p", "将下方示例保存为 UTF-8 文本 SKILL.md（最大 16 KiB），再替换名称、简介和正文。名称只用小写英文字母、数字和连字符。"),
      node("pre", "---\nname: daily-review\ndescription: 核对店铺日报中的数据日期。\n---\n# 日报复核\n先确认统计日期，再区分缺失数据和真实零值。"),
      node("p", "不接受 ZIP、脚本或工具权限声明。可把原始文档导出到支持此格式的其他工具；导入后的权限和效果由该工具决定。"));
    const fileLabel = node("label", "选择本地技能文档");
    const file = node("input");
    file.type = "file";
    file.id = fileLabel.htmlFor = "skill-library-file";
    file.accept = ".md,text/markdown,text/plain";
    const upload = button("上传到资料库");
    upload.className = "button primary";
    const reload = button("刷新");
    const actions = node("div", undefined, "save-bar");
    actions.append(upload, reload);
    const status = node("p", "", "muted");
    status.setAttribute("role", "status");
    const list = node("div");
    const detail = node("section");
    detail.setAttribute("aria-label", "技能详情");
    root.replaceChildren(title, intro, guide, fileLabel, file, actions, status, list, detail);

    const sync = () => {
      file.disabled = busy || !catalog?.can_upload;
      upload.disabled = file.disabled || !file.files?.length || !csrf();
      reload.disabled = busy;
      for (const b of [...list.querySelectorAll("button"), ...detail.querySelectorAll("button")]) {
        b.disabled = busy || !catalog || (b.dataset.write === "yes" && (!catalog.can_upload || !csrf()));
      }
    };
    const reset = () => {
      catalog = selected = null;
      list.replaceChildren();
      detail.replaceChildren();
    };
    function renderList(value) {
      catalog = value;
      list.replaceChildren();
      if (!value.skills.length) {
        list.append(node("p", value.can_upload ? "还没有上传的技能。" : "服务端未配置资料库。"));
      } else {
        const ul = node("ul");
        for (const item of value.skills) {
          const li = node("li");
          const b = button(`${item.name} · v${item.version} · ${item.enabled ? "可用" : "停用"}`);
          b.addEventListener("click", () => run(() => inspect(item.name)));
          li.append(b, node("p", item.description, "muted"));
          ul.append(li);
        }
        list.append(ul);
      }
    }
    function renderDetail(info) {
      selected = info;
      const current = info.document.version === info.current_version;
      const heading = node("h3", `${info.name} · v${info.document.version}${current ? "（当前版本）" : "（历史版本）"}`);
      const state = node("p", `资料库状态：${info.enabled ? "可用" : "停用"}。模型应用状态：未应用。`);
      const preview = node("p", info.local_preview
        ? `本地声明预览：分析目标为「${objectiveLabels[info.local_preview.objective]}」。尚未修改 JX 默认策略。`
        : "此版本没有可预览的已启用 JX 分析目标。", "muted");
      const textLabel = node("label", "原始文档（仅显示纯文本）");
      const source = node("textarea");
      source.id = textLabel.htmlFor = "skill-library-source";
      source.rows = 9;
      source.readOnly = true;
      source.value = info.document.source;
      const checksum = node("p", `SHA-256：${info.document.digest}`, "skill-library-digest muted");
      const choose = node("select");
      const chooseLabel = node("label", "查看版本");
      choose.id = chooseLabel.htmlFor = "skill-library-version";
      for (const v of info.versions) {
        const option = node("option", `v${v.version} · ${dateLabel(v.created)} · ${v.digest.slice(0, 12)}`);
        option.value = String(v.version);
        choose.append(option);
      }
      choose.value = String(info.document.version);
      const view = button("查看所选版本");
      view.addEventListener("click", () => run(() => inspect(info.name, Number(choose.value))));
      const change = button(current ? (info.enabled ? "停用此技能" : "审阅完成，设为可用") : "恢复为当前版本");
      change.dataset.write = "yes";
      change.addEventListener("click", () => {
        if (busy || !catalog?.can_upload || !csrf()) return;
        const action = current ? (info.enabled ? "disable" : "enable") : "rollback";
        const question = action === "rollback"
          ? `将 ${info.name} 恢复到 v${info.document.version}？恢复后保持停用，历史记录不变。`
          : `${auditLabels[action]} ${info.name} v${info.current_version}？此操作仅更改资料库状态。`;
        if (window.confirm(question)) void run(() => mutate(
          { action, name: info.name, ...(current ? {} : { version: info.document.version }) }, info.name));
      });
      const download = button("导出此版本 SKILL.md");
      download.addEventListener("click", () => {
        if (busy || !catalog) return;
        const url = URL.createObjectURL(new Blob([info.document.source], { type: "text/markdown;charset=utf-8" }));
        const link = node("a");
        link.href = url;
        link.download = "SKILL.md";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
      const audit = node("details");
      audit.append(node("summary", "最近操作记录（最多 30 条）"));
      const events = node("ol");
      for (const e of info.audit) events.append(node("li",
        `${dateLabel(e.at)} · 管理员 · ${auditLabels[e.action]} v${e.to_version} · ${e.enabled ? "可用" : "停用"}`));
      audit.append(events);
      detail.replaceChildren(heading, state, preview, chooseLabel, choose, view, textLabel, source, checksum, change, download, audit);
    }
    function dateLabel(raw) {
      const date = new Date(raw);
      return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN") : "";
    }
    async function inspect(name, version) {
      const info = await api(`skill-library/${encodeURIComponent(name)}${version ? "?version=" + version : ""}`);
      if (info.revision !== catalog?.revision) {
        reset();
        throw new Error("资料库已变更，请刷新后重试。");
      }
      renderDetail(info);
      status.textContent = "";
    }
    async function mutate(fields, name) {
      const revision = catalog.revision;
      const value = await api("skill-library", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ ...fields, revision }),
      });
      if (fields.action === "upload" && value.revision === revision) {
        status.textContent = "文件与当前版本相同，未创建新版本；原审阅状态不变。";
        return;
      }
      selected = null;
      detail.replaceChildren();
      renderList(value);
      if (name) await inspect(name);
      status.textContent = fields.action === "upload" ? "已入库。新版本保持停用，点击名称可审阅。" : "资料库已更新；没有应用到模型。";
    }
    async function run(fn) {
      if (busy) return;
      busy = true;
      sync();
      try { await fn(); }
      catch (e) {
        if (e.status === 409 || e.status === 401) reset();
        status.textContent = e.message || "操作失败，请刷新后重试。";
      } finally { busy = false; sync(); }
    }
    function refresh() {
      return run(async () => {
        const previous = selected?.name;
        reset();
        renderList(await api("skill-library"));
        if (previous && catalog.skills.some(s => s.name === previous)) await inspect(previous);
        status.textContent = "";
      });
    }
    file.addEventListener("change", sync);
    reload.addEventListener("click", refresh);
    upload.addEventListener("click", () => run(async () => {
      const chosen = file.files?.[0];
      if (!chosen || !catalog?.can_upload || !csrf()) return;
      if (chosen.name !== "SKILL.md" || chosen.size > catalog.max_bytes)
        throw new Error("请选择名为 SKILL.md 且不超过 16 KiB 的文件。");
      const bytes = new Uint8Array(await chosen.arrayBuffer());
      await mutate({ action: "upload", filename: chosen.name, content_base64: btoa(String.fromCharCode(...bytes)) });
      file.value = "";
    }));
    const panel = { refresh };
    mounts.set(root, panel);
    void refresh();
    return panel;
  }
  return { mount };
})();
