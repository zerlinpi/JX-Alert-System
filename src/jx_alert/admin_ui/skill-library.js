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
  const auditLabels = { upload: "上传", upload_package: "导入技能包", enable: "设为可用", disable: "停用", rollback: "恢复版本" };

  function mount(root, { api, csrf }) {
    if (!root) return null;
    if (mounts.has(root)) return mounts.get(root);
    let catalog, selected, busy = false;
    const title = node("h2", "自定义 Skills 资料库");
    title.id = "skill-library-title";
    const intro = node("p", "选择 SKILL.md 或带有参考文档的 ZIP，审阅后可导出整个版本。入库不会运行文件、调用模型或发送飞书通知。", "muted");
    const guide = node("details", undefined, "skill-format");
    guide.append(node("summary", "第一次上传？查看文件格式与示例"),
      node("p", "将下方示例保存为 UTF-8 文本 SKILL.md（单文件最大 128 KiB），再替换名称、简介和正文。名称只用小写英文字母、数字和连字符。"),
      node("pre", "---\nname: daily-review\ndescription: 核对店铺日报中的数据日期。\n---\n# 日报复核\n先确认统计日期，再区分缺失数据和真实零值。"),
      node("p", "打包时，只保留一个技能的 SKILL.md 和 references/ 中的 UTF-8 .md、.txt 文件。ZIP 最大 576 KiB，最多 32 份文档、解压后共 512 KiB。脚本、素材、工具权限声明不支持；外部链接、其他技能及 MCP 依赖不会自动获取。"));
    const fileLabel = node("label", "选择本地技能文档");
    const file = node("input");
    file.type = "file";
    file.id = fileLabel.htmlFor = "skill-library-file";
    file.accept = ".md,.zip,text/markdown,application/zip";
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
      const references = Object.entries(info.references || {});
      const manifest = node("div");
      manifest.append(node("p", `主文档：SKILL.md；参考文档：${references.length} 份；源文件声明版本：${info.document.declared_version || "未记录"}。`));
      if (references.length) {
        const refLabel = node("label", "查看包内参考文档");
        const ref = node("select");
        ref.id = refLabel.htmlFor = "skill-library-reference";
        const contentsLabel = node("label", "参考文档原文（只读）");
        const contents = node("textarea");
        contents.id = contentsLabel.htmlFor = "skill-library-reference-source";
        contents.readOnly = true;
        contents.rows = 6;
        for (const [i, [path]] of references.entries()) {
          const option = node("option", path);
          option.value = String(i);
          ref.append(option);
        }
        contents.value = references[0][1];
        ref.addEventListener("change", () => { contents.value = references[Number(ref.value)][1]; });
        manifest.append(refLabel, ref, contentsLabel, contents);
      }
      if (info.missing_text_links?.length) {
        const note = node("details");
        note.append(node("summary", "这些本地文档链接不在此版本中（最多显示 20 处）"));
        const missing = node("ul");
        for (const [from, target] of info.missing_text_links)
          missing.append(node("li", `${from} → ${target}`));
        note.append(missing);
        manifest.append(note);
      }
      const checksum = node("p", `版本内容 SHA-256：${info.document.digest}`, "skill-library-digest muted");
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
      const bundle = node("a", "导出完整版本 ZIP（含参考文档）", "button secondary");
      bundle.href = `/console/api/skill-library/${encodeURIComponent(info.name)}/package.zip?version=${info.document.version}`;
      bundle.download = `${info.name}-v${info.document.version}.zip`;
      const audit = node("details");
      audit.append(node("summary", "最近操作记录（最多 30 条）"));
      const events = node("ol");
      for (const e of info.audit) events.append(node("li",
        `${dateLabel(e.at)} · 管理员 · ${auditLabels[e.action]} v${e.to_version} · ${e.enabled ? "可用" : "停用"}`));
      audit.append(events);
      detail.replaceChildren(heading, state, preview, chooseLabel, choose, view, textLabel, source, manifest, checksum, change, download, bundle, audit);
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
      const uploading = fields.action === "upload" || fields.action === "upload_package";
      if (uploading && value.revision === revision) {
        status.textContent = "文件与当前版本相同，未创建新版本；原审阅状态不变。";
        return;
      }
      selected = null;
      detail.replaceChildren();
      renderList(value);
      if (name) await inspect(name);
      status.textContent = uploading ? "已入库。新版本保持停用，点击名称可审阅。" : "资料库已更新；没有应用到模型。";
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
      const zip = /\.zip$/i.test(chosen.name);
      if (!(zip || chosen.name === "SKILL.md"))
        throw new Error("请选择 SKILL.md，或仅含单个技能和参考文档的 ZIP。");
      if (chosen.size > (zip ? catalog.max_archive_bytes : catalog.max_bytes))
        throw new Error("文件过大。SKILL.md 最大 128 KiB，ZIP 最大 576 KiB。");
      const bytes = new Uint8Array(await chosen.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192)
        binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      await mutate({ ...(zip ? { action: "upload_package" } : { action: "upload", filename: chosen.name }),
        content_base64: btoa(binary) });
      file.value = "";
    }));
    const panel = { refresh };
    mounts.set(root, panel);
    void refresh();
    return panel;
  }
  return { mount };
})();
