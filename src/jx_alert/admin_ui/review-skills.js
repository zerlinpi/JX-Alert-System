"use strict";

window.JxReviewSkills = (() => {
  const panels = new WeakMap();
  const el = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  };

  function mount(container, { api, csrf }) {
    if (!container) return null;
    if (panels.has(container)) return panels.get(container);
    let data = null;
    let busy = false;

    const title = el("h2", "JX 分析技能");
    title.id = "review-skills-title";
    const summary = el("p", "安装 JX 内置广告分析策略。不会安装 SparkX 或其他第三方技能，也不会变更广告设置。", "muted");
    const label = el("label", "默认分析策略");
    const picker = el("select", "", "select");
    picker.id = "jx-review-skill";
    label.htmlFor = picker.id;
    const description = el("p", "", "muted");
    const status = el("p", "", "muted");
    status.setAttribute("role", "status");
    const controls = el("div", "", "save-bar");
    const apply = el("button", "安装选中策略", "button primary");
    const reload = el("button", "刷新", "button secondary");
    apply.type = "button";
    reload.type = "button";
    controls.append(apply, reload);
    container.replaceChildren(title, summary, label, picker, description, controls, status);

    function syncControls() {
      const chosen = data?.presets.find((item) => item.id === picker.value);
      description.textContent = chosen?.description || "";
      apply.disabled = busy || !data?.can_install || !chosen || chosen.id === data.active_preset_id;
      picker.disabled = busy || !data?.can_install;
      reload.disabled = busy;
    }

    function render(next) {
      data = next;
      picker.replaceChildren(...next.presets.map(({ id, label: optionLabel }) => {
        const option = el("option", optionLabel);
        option.value = id;
        return option;
      }));
      picker.value = next.active_preset_id || "";
      const { ad_types: byType, campaigns } = next.preserved_overrides;
      status.textContent = {
        default: "正在使用 JX 内置默认规则，尚未保存自定义策略。",
        configured: `策略版本 ${next.catalog_version}。保留 ${byType} 类广告及 ${campaigns} 个活动的现有专属规则。`,
        invalid: "现有配置无效，为保护现有规则，安装已停用。",
        disabled: "服务端未配置分析技能路径。",
      }[next.status] || "无法确认分析技能状态。";
      syncControls();
    }

    async function refresh() {
      if (busy) return;
      busy = true;
      data = null;
      syncControls();
      status.textContent = "正在加载分析技能…";
      try {
        render(await api("review-skills"));
      } catch (error) {
        status.textContent = error.message || "分析技能加载失败。";
      } finally {
        busy = false;
        syncControls();
      }
    }

    async function install() {
      const chosen = data?.presets.find((item) => item.id === picker.value);
      const token = csrf();
      if (apply.disabled || !chosen || !token) return;
      if (!window.confirm(`安装「${chosen.label}」为默认分析策略？现有广告类型和活动专属规则将保留。`)) return;
      busy = true;
      syncControls();
      try {
        render(await api("review-skills", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
          body: JSON.stringify({ revision: data.revision, preset_id: chosen.id }),
        }));
        status.textContent = `已安装「${chosen.label}」。${status.textContent}`;
      } catch (error) {
        if (error.status === 409) data = null;
        status.textContent = error.message || "安装失败，请刷新后重试。";
      } finally {
        busy = false;
        syncControls();
      }
    }

    picker.addEventListener("change", syncControls);
    apply.addEventListener("click", install);
    reload.addEventListener("click", refresh);
    const panel = { refresh };
    panels.set(container, panel);
    void refresh();
    return panel;
  }
  return { mount };
})();
