const byId = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
let state = null;
let busy = false;
let ready = false;
let coreDirty = false;
let selectedPersona = "";
let selectedUser = "";
let selectedScope = "";
let dialogRequest = null;
let configBase = {};
let configSchema = {};
let configRevision = "";
let configDirty = false;
let configConflict = false;
const configControls = new Map();

const array = (value) => (Array.isArray(value) ? value : []);
const object = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
const string = (value) => (value == null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2));
const first = (...values) => values.find((value) => value !== undefined && value !== null && value !== "");
const recordId = (record) => string(first(record.id, record.memory_id, record.style_id, record.version_id));
const recordText = (record) => string(first(record.content, record.text, record.summary, record.expression, record.pattern, record.prompt, record.core));
const memoryStates = { active: "当前有效", asserted: "已陈述", planned: "计划中", in_progress: "进行中", completed: "已完成", cancelled: "已取消" };

function node(tag, text = "", className = "") {
  const result = document.createElement(tag);
  result.textContent = string(text);
  if (className) result.className = className;
  return result;
}

function dateText(value) {
  if (value === undefined || value === null || value === "") return "时间未记录";
  const numeric = typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : value;
  const date = new Date(numeric);
  if (!Number.isFinite(date.getTime())) return string(value);
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function versionText(version) {
  return recordText(version) || string(first(version.snapshot, version.styles, version.data, version));
}

function notice(message, kind = "success") {
  const target = byId("notice");
  target.textContent = message;
  target.className = `notice ${kind}`;
  target.hidden = !message;
  target.setAttribute("role", kind === "error" ? "alert" : "status");
}

function paused() {
  const status = object(state?.status);
  return Boolean(first(status.paused, status.learning_paused, false));
}

function setAvailability() {
  byId("app").setAttribute("aria-busy", String(busy));
  document.querySelectorAll("#app button, #app input, #app textarea, #app select").forEach((control) => {
    if (control.classList.contains("tab")) return;
    control.disabled = busy || !ready;
  });
  byId("user-forget").disabled ||= !selectedUser;
  byId("core-rollback").disabled ||= !byId("core-versions").value;
  byId("style-rollback").disabled ||= !selectedScope || !byId("style-versions").value;
  byId("core-versions").disabled ||= !array(state?.persona?.versions).length;
  byId("style-versions").disabled ||= !array(state?.style_versions).length;
  byId("scope-key").disabled ||= !array(state?.scopes).length;
  byId("config-save").disabled ||= !configDirty || configConflict || !configRevision;
  byId("config-reload").disabled ||= !state?.config;
  document.querySelectorAll(".group-toggle input, .group-tags input").forEach((control) => { control.disabled ||= configConflict || !configRevision; });
  document.querySelectorAll("[data-unavailable]").forEach((control) => { control.disabled = true; });
}

async function perform(work, successMessage = "") {
  if (busy || !ready) return;
  busy = true;
  setAvailability();
  try {
    await work();
    if (successMessage) notice(successMessage);
  } catch (error) {
    notice(error?.message || "操作失败，请稍后重试。", "error");
  } finally {
    busy = false;
    setAvailability();
  }
}

async function loadState({ replaceCore = false } = {}) {
  const params = {};
  if (selectedPersona) params.persona_id = selectedPersona;
  if (selectedUser) params.user_id = selectedUser;
  if (selectedScope) params.scope_key = selectedScope;
  const result = await bridge.apiGet("state", params);
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("管理接口返回了无法识别的数据。");
  state = result;
  selectedPersona = string(first(state.persona?.id, state.persona?.persona_id, selectedPersona));
  byId("persona-id").value = selectedPersona;
  renderState(replaceCore);
}

async function action(actionName, payload = {}, { replaceCore = false } = {}) {
  await bridge.apiPost("action", { action: actionName, persona_id: selectedPersona, user_id: selectedUser, scope_key: selectedScope, ...payload });
  try {
    await loadState({ replaceCore });
  } catch (error) {
    throw new Error(`操作已提交，但刷新未成功。请点击刷新核对结果：${error?.message || "管理接口暂不可用"}`);
  }
}

function renderState(replaceCore) {
  const status = object(state.status);
  const isPaused = paused();
  const disabledByConfig = status.enabled === false || status.learning_enabled === false;
  byId("learning-status").textContent = disabledByConfig ? "配置已关闭学习" : isPaused ? "学习已暂停" : "学习已开启";
  byId("learning-status").className = `status-pill ${isPaused || disabledByConfig ? "paused" : "running"}`;
  byId("learning-toggle").textContent = isPaused ? "恢复学习" : "暂停学习";
  byId("status-detail").textContent = string(status);
  byId("persona-caption").textContent = selectedPersona ? `正在管理：${selectedPersona}` : "后端未返回人格 ID，请检查插件配置。";
  if (!coreDirty || replaceCore) {
    byId("core-content").value = string(first(state.persona?.core, state.persona?.content, ""));
    coreDirty = false;
  }
  renderCoreDirty();
  const versions = array(state.persona?.versions);
  byId("core-version-count").textContent = `${versions.length} 个版本`;
  fillVersions("core", versions);
  renderProfile();
  renderMemories();
  renderScopes();
  renderStyles();
  fillVersions("style", array(state.style_versions));
  renderConfig();
  renderOverview();
}

function renderCoreDirty() {
  const length = byId("core-content").value.length;
  byId("core-dirty").textContent = `${length.toLocaleString("zh-CN")} 字 · ${coreDirty ? "有未保存修改" : "已与当前版本同步"}`;
}

const sectionNames = { general: "基础与群范围", basic: "基础与群范围", scope: "基础与群范围", learning: "学习与采样", budget: "Token 预算", tokens: "Token 预算", schedule: "分时学习", evolution: "进化方向", direction: "进化方向", retention: "保留与高级", advanced: "保留与高级" };
const optionNames = { immediate: "积累够样本即学习", adaptive: "自动判断", windows: "仅指定时段", hybrid: "组合策略" };

function configValueText(value, schema) {
  return schema.type === "list" ? array(value).join("\n") : string(value);
}

function updateConfigDirty({ renderGroups = true } = {}) {
  let count = 0;
  for (const [name, control] of configControls) {
    const changed = configSchema[name].type === "bool" ? control.checked !== configBase[name] : control.value !== configValueText(configBase[name], configSchema[name]);
    if (changed) count += 1;
    control.classList.toggle("changed", changed);
  }
  configDirty = count > 0;
  byId("config-dirty").textContent = configConflict ? "后台配置已改变 · 草稿保留中" : count ? `${count} 项未保存修改` : configRevision ? "设置已同步" : "尚未载入配置";
  byId("config-conflict").hidden = !configConflict;
  byId("settings-tab-dot").hidden = !configDirty;
  if (renderGroups) renderGroupPicker();
  setAvailability();
}

function groupEntries(field) {
  return [...new Set(string(configControls.get(field)?.value).split(/\r?\n/).map((value) => value.trim()).filter(Boolean))];
}

function groupMatches(field, group) {
  const entries = groupEntries(field);
  return entries.includes(group.group_id) || entries.includes(`${group.platform_id}:${group.group_id}`);
}

function setGroupEntry(field, group, selected) {
  const control = configControls.get(field);
  if (!control) return;
  const entries = new Set(groupEntries(field));
  const key = `${group.platform_id}:${group.group_id}`;
  if (selected) {
    if (!entries.has(group.group_id)) entries.add(key);
  } else {
    entries.delete(key);
    if (entries.delete(group.group_id)) {
      // Turn a legacy all-connections rule into the other known connections.
      // Never remove a different group's selection or implicitly enable new ones.
      array(state?.groups).filter((row) => row.group_id === group.group_id && row.platform_id !== group.platform_id)
        .forEach((row) => entries.add(`${row.platform_id}:${row.group_id}`));
    }
  }
  control.value = [...entries].sort().join("\n");
}

function groupTagText(group) {
  const entries = groupEntries("group_tags");
  const key = `${group.platform_id}:${group.group_id}=`;
  const found = entries.find((entry) => entry.startsWith(key)) || entries.find((entry) => entry.startsWith(`${group.group_id}=`));
  return found ? found.slice(found.indexOf("=") + 1) : "";
}

function setGroupTags(group, value) {
  const control = configControls.get("group_tags");
  if (!control) return;
  const key = `${group.platform_id}:${group.group_id}`;
  const entries = groupEntries("group_tags");
  const legacy = entries.find((entry) => entry.startsWith(`${group.group_id}=`));
  const kept = entries.filter((entry) => !entry.startsWith(`${key}=`) && !entry.startsWith(`${group.group_id}=`));
  if (legacy) {
    array(state?.groups).filter((row) => row.group_id === group.group_id && row.platform_id !== group.platform_id).forEach((row) => {
      const other = `${row.platform_id}:${row.group_id}=`;
      if (!kept.some((entry) => entry.startsWith(other))) kept.push(other + legacy.slice(legacy.indexOf("=") + 1));
    });
  }
  if (value.trim()) kept.push(`${key}=${value.trim()}`);
  control.value = kept.sort().join("\n");
}

function renderGroupPicker() {
  const target = byId("group-list");
  target.replaceChildren();
  const query = byId("group-search").value.trim().toLocaleLowerCase();
  const filter = byId("group-filter").value || "all";
  const groups = array(state?.groups);
  const flags = (group) => ({
    observation: groupMatches("observation_groups", group),
    memory: groupMatches("memory_groups", group) && !groupMatches("memory_disabled_groups", group),
    global: groupMatches("global_style_groups", group),
  });
  const selectedCount = groups.filter((group) => Object.values(flags(group)).some(Boolean)).length;
  byId("group-count").textContent = `${groups.length} 个群 · 已选 ${selectedCount}`;
  const globalDisabled = configControls.get("enabled")?.checked === false || configControls.get("learning_enabled")?.checked === false || paused();
  byId("group-learning-note").textContent = globalDisabled
    ? "当前总开关关闭或学习已暂停。可先选择群，恢复学习后才会收集新样本。"
    : configControls.get("memory_enabled")?.checked === false
      ? "互动记忆总开关已关闭，已选的互动记忆群暂不生效。"
      : "旁听与互动控制采集。全局梗独立控制跨群使用：不启用采集，也不开放白名单；按本群标签筛选同人格已启用表达。本群表达优先。";
  const visible = groups.filter((group) => {
    const selected = Object.values(flags(group)).some(Boolean);
    return (!query || `${group.group_name || ""} ${group.group_id} ${group.platform_id} ${group.bot_id} ${groupTagText(group)}`.toLocaleLowerCase().includes(query))
      && (filter === "all" || (filter === "selected" ? selected : !selected));
  });
  if (!visible.length) {
    target.append(node("p", groups.length ? "没有符合搜索条件的群。" : "尚未发现群。机器人收到群消息后会自动列出；发现群无需先开启学习。", "empty"));
    return;
  }
  visible.forEach((group) => {
    const card = node("article", "", "group-item");
    const title = node("div", "", "group-title");
    title.append(node("h4", group.group_name || "群名待获取"));
    const enabled = flags(group);
    title.append(node("span", Object.values(enabled).some(Boolean) ? "已选择" : "仅登记", "tag"));
    const meta = node("p", `群号 ${group.group_id} · 连接 ${group.platform_id} · 机器人 ${group.bot_id}`, "muted small");
    const seen = node("p", `首次发现：${dateText(group.first_seen)} · 最近发现：${dateText(group.last_seen)}${counter(group.member_count) !== null ? ` · ${group.member_count} 人` : ""}`, "muted small");
    const toggles = node("div", "", "group-toggles");
    for (const [kind, field, labelText] of [["observation", "observation_groups", "旁听学梗"], ["memory", "memory_groups", "互动记忆"], ["global", "global_style_groups", "允许使用全局梗"]]) {
      const label = node("label", "", "group-toggle");
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.id = `group-${kind}-${encodeURIComponent(group.platform_id)}-${group.group_id}`;
      checkbox.checked = enabled[kind];
      checkbox.disabled = busy || !ready || configConflict || !configRevision;
      checkbox.setAttribute("aria-label", `${group.group_name || group.group_id} ${group.platform_id} ${labelText}`);
      checkbox.addEventListener("change", () => {
        if (busy || !ready || configConflict || !configRevision) return;
        setGroupEntry(field, group, checkbox.checked);
        if (kind === "memory" && checkbox.checked) setGroupEntry("memory_disabled_groups", group, false);
        const focusId = checkbox.id;
        updateConfigDirty();
        byId(focusId)?.focus();
      });
      label.append(checkbox, node("span", labelText));
      toggles.append(label);
    }
    card.append(title, meta, seen, toggles);
    const tagField = node("div", "", "field group-tags");
    const tagInput = node("input");
    tagInput.type = "text";
    tagInput.id = `group-tags-${encodeURIComponent(group.platform_id)}-${group.group_id}`;
    tagInput.value = groupTagText(group);
    tagInput.placeholder = "如：太鼓达人、轻松、技术交流";
    tagInput.maxLength = 310;
    tagInput.disabled = busy || !ready || configConflict || !configRevision;
    const tagLabel = node("label", "群标签（性格、话题与能力领域）");
    tagLabel.htmlFor = tagInput.id;
    const hint = node("p", "最多12个标签，以逗号分隔。匹配梗的含义和适用语境，避用语境优先；未填标签时不使用外群梗。", "field-hint");
    tagInput.addEventListener("input", () => {
      if (busy || !ready || configConflict || !configRevision) return;
      setGroupTags(group, tagInput.value);
      updateConfigDirty({ renderGroups: false });
    });
    tagField.append(tagLabel, tagInput, hint);
    card.append(tagField);
    if (!group.group_name || group.name_status === "unavailable") card.append(node("p", group.group_name ? "群名为上次缓存；信息查询暂不可用，后续消息会触发重试。" : "群名尚未取得，后续消息会触发查询；仍可按群号选择。", "field-hint"));
    target.append(card);
  });
}

function renderConfig({ replace = false } = {}) {
  const incoming = object(state?.config);
  if (!incoming.values || !incoming.schema || !incoming.revision) {
    if (!configControls.size) byId("settings-sections").replaceChildren(node("p", "完整配置尚不可用，请确认后端插件已更新。", "empty"));
    return;
  }
  if (configDirty && !replace) {
    configConflict = incoming.revision !== configRevision;
    updateConfigDirty();
    return;
  }
  configBase = JSON.parse(JSON.stringify(incoming.values));
  configSchema = object(incoming.schema);
  configRevision = incoming.revision;
  configConflict = false;
  configDirty = false;
  configControls.clear();
  const target = byId("settings-sections");
  target.replaceChildren();
  const sections = new Map();
  const sectionCards = new Map();
  for (const [name, schemaValue] of Object.entries(configSchema)) {
    const schema = object(schemaValue);
    const sectionKey = schema.section || "advanced";
    if (!sections.has(sectionKey)) {
      const section = node("section", "", "card settings-section");
      const title = node("h3", sectionNames[sectionKey] || sectionKey);
      const fields = node("div", "", "settings-fields");
      section.append(title, fields);
      sections.set(sectionKey, fields);
      sectionCards.set(sectionKey, section);
      target.append(section);
    }
    const field = node("div", "", `field config-field${schema.type === "list" || name === "evolution_guidance" ? " wide-field" : ""}`);
    const label = node("label", schema.description || name);
    label.htmlFor = `setting-${name}`;
    let control;
    if (schema.type === "bool") {
      control = node("input");
      control.type = "checkbox";
      control.checked = configBase[name] === true;
      field.classList.add("check-field");
    } else if (Array.isArray(schema.options) && schema.options.length) {
      control = node("select");
      schema.options.forEach((item) => {
        const value = typeof item === "object" ? first(item.value, item.id) : item;
        const option = node("option", typeof item === "object" ? first(item.label, item.description, value) : optionNames[item] || item);
        option.value = string(value);
        control.append(option);
      });
      control.value = configValueText(configBase[name], schema);
    } else if (schema.type === "list" || name === "evolution_guidance" || schema.type === "text") {
      control = node("textarea");
      control.rows = name === "evolution_guidance" ? 9 : 3;
      control.value = configValueText(configBase[name], schema);
      if (schema.type === "list") control.placeholder = "每行一项；留空表示空列表";
      if (name === "evolution_guidance") {
        control.maxLength = 4000;
        control.placeholder = "写下希望可可子逐渐形成的表达习惯、学习重点，以及应当避免的倾向……";
      }
    } else {
      control = node("input");
      control.type = schema.type === "int" ? "number" : "text";
      control.value = configValueText(configBase[name], schema);
      if (schema.type === "int") {
        control.step = "1";
        if (schema.minimum !== undefined) control.min = schema.minimum;
        if (schema.maximum !== undefined) control.max = schema.maximum;
      }
      const choices = name === "persona_id" ? array(state.choices?.personas) : name === "learning_provider_id" ? array(state.choices?.providers) : [];
      if (choices.length) {
        const datalist = node("datalist");
        datalist.id = `choices-${name}`;
        choices.forEach((choice) => {
          const option = node("option");
          option.value = string(choice.id);
          option.label = string(choice.label || choice.id);
          datalist.append(option);
        });
        control.setAttribute("list", datalist.id);
        field.append(datalist);
      }
    }
    control.id = `setting-${name}`;
    control.name = name;
    control.autocomplete = "off";
    control.addEventListener(schema.type === "bool" || control.tagName === "SELECT" ? "change" : "input", updateConfigDirty);
    field.append(label, control);
    if (schema.hint) field.append(node("p", schema.hint, "field-hint"));
    if (name === "evolution_guidance") {
      const example = mutationButton("载入参考方向", async () => {
        if (control.value.trim() && !await ask({ title: "载入参考方向", description: "将替换编辑框里的内容，保存设置后才生效。", submit: "替换草稿" })) return;
        control.value = "学习简短、自然、有分寸的群友表达，少复读和固定口癖。认真问题先解决问题，再适量调侃。优先保留群友明确的太鼓练习目标、进展和回答偏好；术数解读保持清楚、有依据。不要把玩笑、猜测或第三方说法当事实。";
        updateConfigDirty();
        notice("参考方向已载入草稿，保存设置后生效。", "info");
      });
      field.append(node("p", "方向引导学习取舍与表达，不自动改写固定人设，不增加工具能力。", "field-hint"), example);
    }
    configControls.set(name, control);
    if (["observation_groups", "memory_groups", "memory_disabled_groups", "global_style_groups", "group_tags"].includes(name)) {
      const advanced = node("details", "", "wide-field group-advanced");
      advanced.append(node("summary", `高级：${schema.description}（与群开关同步）`), field);
      sections.get(sectionKey).append(advanced);
    } else sections.get(sectionKey).append(field);
  }
  const order = ["基础与范围", "基础与群范围", "general", "basic", "scope", "进化方向", "evolution", "direction", "Token 预算", "budget", "tokens", "价格时段与积累", "schedule", "学习与模型", "learning", "保留与高级", "retention", "advanced"];
  const keys = [...sectionCards.keys()].sort((a, b) => (order.indexOf(a) < 0 ? 99 : order.indexOf(a)) - (order.indexOf(b) < 0 ? 99 : order.indexOf(b)));
  target.replaceChildren(...keys.map((key) => sectionCards.get(key)));
  const nav = byId("settings-nav");
  nav.replaceChildren();
  keys.forEach((key, index) => {
    const section = sectionCards.get(key);
    section.id = `config-section-${index}`;
    const link = node("a", sectionNames[key] || key);
    link.href = `#${section.id}`;
    nav.append(link);
  });
  byId("settings-count").textContent = `${configControls.size} 项设置`;
  updateConfigDirty();
}

function changedConfigValues() {
  const changes = {};
  for (const [name, control] of configControls) {
    const schema = configSchema[name];
    let value;
    if (schema.type === "bool") value = control.checked;
    else if (schema.type === "int") {
      if (!/^-?\d+$/.test(control.value.trim())) throw new Error(`${schema.description || name}：请填写整数。`);
      value = Number(control.value);
      if (!Number.isSafeInteger(value) || (schema.minimum !== undefined && value < schema.minimum) || (schema.maximum !== undefined && value > schema.maximum)) throw new Error(`${schema.description || name}：超出允许范围 ${schema.minimum ?? "不限"}～${schema.maximum ?? "不限"}。`);
    } else if (schema.type === "list") value = [...new Set(control.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
    else value = control.value.trim();
    if (name === "evolution_guidance" && value.length > 4000) throw new Error("进化方向最多 4000 字。");
    if (JSON.stringify(value) !== JSON.stringify(configBase[name])) changes[name] = value;
  }
  return changes;
}

async function saveSettings() {
  if (busy || !ready || configConflict || !configRevision) return;
  let values;
  try { values = changedConfigValues(); } catch (error) { notice(error.message, "error"); return; }
  if (!Object.keys(values).length) { notice("设置没有变化。", "info"); return; }
  const changesPersona = values.persona_id !== undefined && values.persona_id !== selectedPersona;
  if (changesPersona && coreDirty && !await ask({ title: "更改目标人格", description: "保存成功后将载入新目标人格，当前未保存的核心人设草稿会被丢弃。", submit: "保存并切换" })) return;
  await perform(async () => {
    let result;
    try { result = await bridge.apiPost("action", { action: "config_save", revision: configRevision, values }); }
    catch (error) {
      if (error?.status === 409 || /冲突|已变化|已更新|已改变|已被.*修改|被.*覆盖|conflict|revision/i.test(error?.message || "")) {
        configConflict = true;
        updateConfigDirty();
      }
      throw error;
    }
    if (result?.saved !== true) throw new Error("后台未确认配置保存成功，草稿已保留。");
    configDirty = false;
    configRevision = "";
    if (changesPersona) { selectedPersona = ""; selectedUser = ""; selectedScope = ""; coreDirty = false; }
    try { await loadState({ replaceCore: changesPersona }); }
    catch (error) { configDirty = true; throw new Error(`配置已保存，但刷新失败；请重载核对结果，暂勿重复保存：${error?.message || "连接中断"}`); }
  }, "设置已保存，后续学习将使用新配置。");
}

const counter = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const countText = (value) => counter(value) === null ? "—" : value.toLocaleString("zh-CN");
const jobNames = { success: "已完成", completed: "已完成", failed: "失败", error: "失败", cancelled: "已取消", discarded: "结果已作废", reserved: "已预留", running: "学习中", started: "学习中", pending: "待确认", released: "未调用", unknown: "用量待确认" };
const scheduleNames = { idle: "等待样本", disabled: "已关闭", paused: "已暂停", learning: "学习中", running: "学习中", cooldown: "间隔等待", budget: "预算不足", budget_exhausted: "预算不足", outside_window: "允许时段外", waiting_samples: "积累样本", insufficient_samples: "积累样本", insufficient: "积累样本", cheap: "低价时段", regular: "常规时段", blocked: "权限受限", deferred: "暂缓学习", complete: "学习完成", completed: "学习完成", success: "学习完成", error: "检查失败", failed: "学习失败", cancelled: "已取消", discarded: "结果作废" };

function renderOverview() {
  const usage = object(state?.usage);
  const totals = object(usage.summary);
  byId("usage-day").textContent = usage.day ? `${usage.day} · 北京时间` : "北京时间";
  const metrics = byId("usage-metrics");
  metrics.replaceChildren();
  for (const [label, value, note, style] of [
    ["已报告 Token", totals.reported_tokens, `输入 ${countText(totals.input_tokens)} / 输出 ${countText(totals.output_tokens)}`, ""],
    ["估算占用", totals.estimated_tokens, `${countText(totals.unknown_calls)} 次未获完整用量`, ""],
    ["在途预留", totals.reserved_tokens, "等待完成或用量确认", ""],
    ["学习调用", totals.calls, `失败 ${countText(totals.failed_calls)} 次`, "accent-metric"],
  ]) {
    const card = node("article", "", `card metric ${style}`);
    card.append(node("span", label, "metric-label"), node("strong", countText(value), "metric-number"), node("span", note, "muted small"));
    metrics.append(card);
  }
  const cfg = object(state?.config?.values);
  const budget = byId("budget-bars");
  budget.replaceChildren();
  for (const [label, used, limit] of [["Token 预算", totals.charged_tokens, first(usage.limits?.daily_tokens, cfg.daily_token_limit)], ["调用预算", totals.calls, first(usage.limits?.daily_calls, cfg.daily_calls)]]) {
    const line = node("div", "", "budget-line");
    const head = node("div", "", "item-header");
    head.append(node("span", label), node("strong", `${countText(used)} / ${countText(limit)}`));
    const track = node("div", "", "budget-track");
    const fill = node("div", "", "budget-fill");
    const ratio = counter(used) !== null && counter(limit) !== null && limit > 0 ? Math.min(100, used / limit * 100) : 0;
    fill.style.width = `${ratio}%`;
    track.append(fill);
    line.append(head, track);
    if (limit === 0) line.append(node("span", "此预算为 0，后台学习不会发起调用。", "muted small"));
    budget.append(line);
  }
  const schedule = object(state.schedule);
  const code = string(first(schedule.status, schedule.state, "idle"));
  byId("schedule-status").textContent = scheduleNames[code] || code;
  byId("schedule-reason").textContent = string(first(schedule.reason, schedule.message, "后台尚未开始检查，或暂无可处理样本。"));
  const details = byId("schedule-details");
  details.replaceChildren();
  const phase = first(schedule.phase, schedule.price_period, schedule.price_band);
  addMeta(details, "当前价段", phase === "cheap" ? "低价" : phase ? "常规" : undefined);
  addMeta(details, "待处理", first(schedule.pending_count, state.status?.pending_sources));
  addMeta(details, "最近学习", state.status?.last_learning_at ? dateText(state.status.last_learning_at) : "暂无");
  if (schedule.next_at || schedule.next_check_at) addMeta(details, "下次可处理", dateText(first(schedule.next_check_at, schedule.next_at)));
  const groups = byId("schedule-groups");
  groups.replaceChildren();
  for (const group of array(schedule.groups).slice(0, 12)) {
    const row = node("div", "", "schedule-group");
    const text = `${group.group_id || "群"} · ${group.purpose === "observation" ? "群表达" : "相处记忆"}`;
    row.append(node("span", text), node("span", `${group.phase === "cheap" ? "低价" : "常规"} · ${scheduleNames[group.state] || group.state || "等待"}`, "muted small"));
    if (group.sample_count != null) row.append(node("small", `样本 ${group.sample_count}${group.minimum == null ? "" : ` / 门槛 ${group.minimum}`}`, "muted"));
    if (group.reason) row.append(node("small", group.reason, "muted"));
    groups.append(row);
  }
  groups.hidden = !groups.children.length;
  byId("direction-preview").textContent = cfg.evolution_guidance ? `当前进化方向：${string(cfg.evolution_guidance).slice(0, 260)}${cfg.evolution_guidance.length > 260 ? "…" : ""}` : "尚未设置进化方向，可在“设置与方向”中填写。";
  renderUsageChart(array(usage.daily));
  renderUsageGroups(array(usage.groups));
  renderRecentLearning(array(usage.recent));
}

function renderUsageChart(rows) {
  const target = byId("usage-chart");
  target.replaceChildren();
  const days = rows.slice(-7);
  if (!days.length) { target.append(node("p", "开始学习后，这里显示每日预算占用。", "empty")); return; }
  const largest = Math.max(1, ...days.map((day) => counter(day.charged_tokens) || 0));
  for (const day of days) {
    const row = node("div", "", "chart-row");
    const track = node("div", "", "chart-track");
    const bar = node("div", "", "chart-fill");
    bar.style.width = `${(counter(day.charged_tokens) || 0) / largest * 100}%`;
    track.append(bar);
    row.append(node("span", string(day.day).slice(5), "chart-date"), track, node("span", countText(day.charged_tokens), "chart-value"));
    target.append(row);
  }
  byId("usage-chart-note").textContent = "统计含保守估算；模型未报告用量时不会显示为已测得的零消耗。";
}

function renderUsageGroups(rows) {
  const target = byId("usage-groups");
  target.replaceChildren();
  if (!rows.length) { target.append(node("p", "今天暂无各群学习用量。", "empty")); return; }
  const table = node("table", "", "usage-table");
  const head = node("tr");
  for (const label of ["群", "调用", "已报告", "预算占用"]) head.append(node("th", label));
  const thead = node("thead");
  thead.append(head);
  const tbody = node("tbody");
  for (const group of rows) {
    const row = node("tr");
    const label = node("td", first(group.group_id, group.label, "未标记群"));
    label.append(node("small", group.platform_id || "", "muted"));
    row.append(label, node("td", countText(group.calls)), node("td", countText(group.reported_tokens)), node("td", countText(group.charged_tokens)));
    tbody.append(row);
  }
  table.append(thead, tbody);
  target.append(table);
}

function renderRecentLearning(rows) {
  const target = byId("learning-recent");
  target.replaceChildren();
  if (!rows.length) { target.append(node("p", "尚无学习记录。配置范围、预算与学习时段后，结果会显示在这里。", "empty")); return; }
  for (const job of rows.slice(0, 20)) {
    const row = node("article", "", "learning-row");
    const title = node("div", "", "item-header");
    title.append(node("strong", job.purpose === "observation" ? "群表达总结" : "相处记忆整理"), node("span", jobNames[job.status] || job.status || "待确认", "tag"));
    const meta = node("div", "", "item-meta");
    addMeta(meta, "时间", dateText(first(job.created_at, job.started_at)));
    addMeta(meta, "群", job.group_id);
    const reported = first(job.reported_tokens, job.total_tokens);
    addMeta(meta, "用量", reported == null ? `未报告 · 预算占用 ${countText(job.charged_tokens)}` : `已报告 ${countText(reported)}`);
    if (job.input_tokens != null || job.output_tokens != null) addMeta(meta, "输入 / 输出", `${countText(job.input_tokens)} / ${countText(job.output_tokens)}`);
    let summary = typeof job.summary === "string" ? job.summary : "";
    if (!summary && job.summary && typeof job.summary === "object") {
      const detail = job.summary;
      const labels = { memories: "采纳记忆", memory_count: "采纳记忆", styles: "采纳表达", style_count: "采纳表达", source_count: "样本", reason: "说明" };
      summary = Object.entries(detail).map(([key, value]) => `${labels[key] || key}：${string(value)}`).join(" · ");
    }
    if (!summary && job.metadata && typeof job.metadata === "object") {
      const detail = job.metadata;
      const parts = [];
      if (detail.sample_count != null) parts.push(`样本 ${detail.sample_count} 条`);
      if (detail.accepted_memories != null) parts.push(`采纳记忆 ${detail.accepted_memories} 条`);
      if (detail.accepted_styles != null) parts.push(`采纳表达 ${detail.accepted_styles} 条`);
      if (detail.phase) parts.push(detail.phase === "cheap" ? "低价时段" : "常规时段");
      summary = parts.join(" · ");
    }
    if (job.error_type) addMeta(meta, "错误类型", job.error_type);
    row.append(title, meta, node("p", summary || "用量与处理状态已记录。", "muted small"));
    target.append(row);
  }
}

function fillVersions(kind, versions) {
  const select = byId(`${kind}-versions`);
  const previous = select.value;
  select.replaceChildren();
  if (!versions.length) {
    const blank = node("option", "暂无版本");
    blank.value = "";
    select.append(blank);
  }
  versions.forEach((version, index) => {
    const option = node("option", `${first(version.label, version.version, `版本 ${versions.length - index}`)} · ${dateText(first(version.created_at, version.timestamp))}`);
    option.value = recordId(version);
    select.append(option);
  });
  if (versions.some((version) => recordId(version) === previous)) select.value = previous;
  renderVersion(kind);
}

function renderVersion(kind) {
  const versions = kind === "core" ? array(state?.persona?.versions) : array(state?.style_versions);
  const chosen = versions.find((version) => recordId(version) === byId(`${kind}-versions`).value);
  byId(`${kind}-version-preview`).textContent = chosen ? versionText(chosen) : "暂无可预览的版本。";
}

function detailsFor(record) {
  const details = node("details");
  details.append(node("summary", "查看来源与完整记录"), node("pre", string(record)));
  return details;
}

function renderProfile() {
  const target = byId("profile");
  target.replaceChildren();
  const profile = object(state.profile);
  target.hidden = !selectedUser || !Object.keys(profile).length;
  if (target.hidden) return;
  target.append(node("h3", `用户 ${selectedUser}`));
  const grid = node("div", "", "profile-grid");
  const names = { nickname: "称呼", display_name: "显示名称", summary: "档案摘要", preferences: "偏好", updated_at: "更新时间", user_id: "用户 ID", memory_count: "记忆条数", items: "当前认知", relationship: "相处记录" };
  Object.entries(profile).forEach(([key, value]) => {
    const item = node("div");
    let display = key.endsWith("_at") ? dateText(value) : string(value);
    if (key === "items" && Array.isArray(value)) display = value.map(recordText).filter(Boolean).join("\n") || "暂无当前认知";
    if (key === "relationship" && value && typeof value === "object") {
      const labels = { interaction_count: "相处次数", group_count: "相处群数", first_seen: "首次相处", last_seen: "最近相处", first_seen_at: "首次相处", last_seen_at: "最近相处", familiarity: "熟悉程度" };
      display = Object.entries(value).map(([field, detail]) => `${labels[field] || field}：${/seen/.test(field) ? dateText(detail) : string(detail)}`).join("\n") || "暂无相处记录";
    }
    item.append(node("span", names[key] || key, "muted small"), node("p", display, "profile-value"));
    grid.append(item);
  });
  target.append(grid);
}

function addMeta(container, label, value) {
  if (value !== undefined && value !== null && value !== "") container.append(node("span", `${label}：${string(value)}`));
}

function mutationButton(text, handler, { danger = false, available = true } = {}) {
  const button = node("button", text, danger ? "danger" : "");
  button.type = "button";
  if (!available) button.dataset.unavailable = "true";
  button.addEventListener("click", handler);
  return button;
}

function renderMemories() {
  const target = byId("memories");
  target.replaceChildren();
  const memories = selectedUser ? array(state.memories) : [];
  byId("memory-count").textContent = `${memories.length} 条记忆`;
  if (!memories.length) {
    target.append(node("p", selectedUser ? "没有找到该用户的记忆。" : "输入 QQ 号后查看该用户的记忆。", "empty"));
    return;
  }
  memories.forEach((memory) => {
    const corrected = memory.status === "corrected";
    const card = node("article", "", "card memory-card");
    const head = node("div", "", "item-header");
    const eventTime = first(memory.time_text, memory.time_expression, memory.event_time);
    const start = first(memory.event_start, memory.occurred_at);
    const end = memory.event_end;
    const timeRange = start ? `${dateText(start)}${end && end !== start ? ` — ${dateText(end)}` : ""}` : "发生时间未明确";
    head.append(node("strong", eventTime || timeRange), node("span", first(memory.kind, memory.category, memory.type, "记忆"), "tag"));
    const meta = node("div", "", "item-meta");
    addMeta(meta, "学习于", dateText(first(memory.learned_at, memory.created_at, memory.recorded_at)));
    addMeta(meta, "消息时间", memory.source_sent_at ? dateText(memory.source_sent_at) : undefined);
    if (eventTime && start) addMeta(meta, "解析时间", timeRange);
    addMeta(meta, "来源群", first(memory.source_group_id, memory.source_group, memory.group_id, memory.scope_key));
    addMeta(meta, "来源", first(memory.source_type, typeof memory.source === "string" ? memory.source : undefined));
    addMeta(meta, "状态", memoryStates[memory.state] || first(memory.state, memory.status));
    if (corrected) addMeta(meta, "记录", "已被纠正");
    addMeta(meta, "置信度", memory.confidence);
    const actions = node("div", "", "item-actions");
    actions.append(mutationButton("纠正", () => correctMemory(memory), { available: Boolean(recordId(memory)) && !corrected }), mutationButton("忘记", () => forgetMemory(memory), { danger: true, available: Boolean(recordId(memory)) }));
    card.append(head, node("p", recordText(memory), "item-content"), meta, detailsFor(memory), actions);
    target.append(card);
  });
}

function renderScopes() {
  const select = byId("scope-key");
  select.replaceChildren();
  const blank = node("option", "请选择群");
  blank.value = "";
  select.append(blank);
  array(state.scopes).forEach((scope) => {
    const key = string(scope.scope_key);
    const label = first(scope.label, scope.group_name, scope.group_id, key);
    const option = node("option", `${label}${scope.platform_id ? ` · ${scope.platform_id}` : ""}`);
    option.value = key;
    select.append(option);
  });
  select.value = selectedScope;
}

function renderStyles() {
  const target = byId("styles");
  target.replaceChildren();
  const styles = selectedScope ? array(state.styles) : [];
  byId("style-count").textContent = `${styles.length} 条表达`;
  if (!styles.length) {
    target.append(node("p", selectedScope ? "这个群还没有学习到表达习惯。" : "选择一个群后查看表达习惯。", "empty"));
    return;
  }
  styles.forEach((style) => {
    const disabled = style.enabled === false || style.disabled === true || style.status === "disabled";
    const card = node("article", "", "card");
    const head = node("div", "", "item-header");
    head.append(node("strong", first(style.name, style.meaning, style.situation, style.context, "表达习惯")), node("span", disabled ? "已停用" : style.status === "active" ? "已启用 · 可供全局匹配" : style.status === "candidate" ? "候选 · 尚不使用" : first(style.status, "使用中"), "tag"));
    const meta = node("div", "", "item-meta");
    addMeta(meta, "学习时间", dateText(first(style.created_at, style.learned_at)));
    addMeta(meta, "适用情境", first(style.contexts, style.situation, style.context));
    addMeta(meta, "避免用于", style.avoid);
    addMeta(meta, "使用次数", first(style.use_count, style.count));
    card.append(head, node("p", recordText(style), "item-content"), meta, detailsFor(style));
    if (!disabled) {
      const actions = node("div", "", "item-actions");
      actions.append(mutationButton("停用此表达", async () => {
        const confirmed = await ask({ title: "停用群表达", description: "这条表达将停止用于本群及全局引用。已有历史版本会保留。", submit: "停用", danger: true });
        if (confirmed) await perform(() => action("style_disable", { style_id: recordId(style) }), "已停用这条表达。");
      }, { danger: true, available: Boolean(recordId(style)) }));
      card.append(actions);
    }
    target.append(card);
  });
}

function ask(options) {
  if (dialogRequest) return Promise.resolve(null);
  const dialog = byId("action-dialog");
  byId("dialog-title").textContent = options.title;
  byId("dialog-description").textContent = options.description;
  byId("dialog-submit").textContent = options.submit || "确认";
  byId("dialog-submit").className = options.danger ? "primary danger" : "primary";
  byId("dialog-content-field").hidden = !options.edit;
  byId("dialog-state-field").hidden = !options.edit;
  byId("dialog-time-field").hidden = !options.edit;
  byId("dialog-confirm-field").hidden = !options.confirmText;
  byId("dialog-content").value = options.content || "";
  byId("dialog-state").value = Object.hasOwn(memoryStates, options.state) ? options.state : "asserted";
  byId("dialog-time").value = options.time || "";
  byId("dialog-confirm").value = "";
  byId("dialog-error").hidden = true;
  return new Promise((resolve) => {
    dialogRequest = { ...options, resolve };
    dialog.showModal();
    (options.edit ? byId("dialog-content") : options.confirmText ? byId("dialog-confirm") : byId("dialog-cancel")).focus();
  });
}

function finishDialog(result) {
  const request = dialogRequest;
  dialogRequest = null;
  byId("action-dialog").close();
  request?.resolve(result);
}

async function correctMemory(memory) {
  if (memory.status === "corrected") return;
  const originalTime = string(first(memory.time_text, memory.time_expression)).trim();
  const result = await ask({ title: "纠正记忆", description: "核对正文和状态。时间未修改或留空时保留原时间；要更改时间，建议填写明确日期。", submit: "保存修正", edit: true, content: recordText(memory), state: memory.state, time: originalTime });
  if (!result) return;
  const changedTime = result.time && result.time !== originalTime ? result.time : null;
  await perform(() => action("memory_correct", { memory_id: recordId(memory), content: result.content, state: result.state, time_expression: changedTime }), "记忆已修正。");
}

async function forgetMemory(memory) {
  const confirmed = await ask({ title: "忘记这条记忆", description: recordText(memory) || "该条记忆将被删除。", submit: "确认忘记", danger: true });
  if (confirmed) await perform(() => action("memory_forget", { memory_id: recordId(memory) }), "已忘记这条记忆。");
}

document.querySelectorAll("[data-panel]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-panel]").forEach((tab) => {
      const active = tab === button;
      tab.classList.toggle("active", active);
      if (active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
      byId(`panel-${tab.dataset.panel}`).hidden = !active;
    });
  });
});

byId("dialog-cancel").addEventListener("click", () => finishDialog(null));
byId("action-dialog").addEventListener("cancel", (event) => { event.preventDefault(); finishDialog(null); });
byId("dialog-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!dialogRequest) return;
  const content = byId("dialog-content").value.trim();
  const state = byId("dialog-state").value;
  let error = "";
  if (dialogRequest.edit && !content) error = "请填写修正后的记忆内容。";
  if (dialogRequest.edit && !Object.hasOwn(memoryStates, state)) error = "请选择有效的记忆状态。";
  if (dialogRequest.confirmText && byId("dialog-confirm").value.trim() !== dialogRequest.confirmText) error = "QQ 号不匹配，请核对后再确认。";
  byId("dialog-error").textContent = error;
  byId("dialog-error").hidden = !error;
  if (!error) finishDialog({ content, state, time: byId("dialog-time").value.trim() });
});

byId("core-content").addEventListener("input", () => { coreDirty = true; renderCoreDirty(); });
byId("core-versions").addEventListener("change", () => { renderVersion("core"); setAvailability(); });
byId("style-versions").addEventListener("change", () => { renderVersion("style"); setAvailability(); });
byId("refresh").addEventListener("click", () => perform(() => loadState(), coreDirty || configDirty ? "已刷新，未保存的草稿已保留。" : "已刷新。"));
byId("settings-form").addEventListener("submit", (event) => { event.preventDefault(); return saveSettings(); });
byId("group-search").addEventListener("input", renderGroupPicker);
byId("group-filter").addEventListener("change", renderGroupPicker);
byId("config-reload").addEventListener("click", async () => {
  if ((configDirty || coreDirty) && !await ask({ title: "重载后台配置", description: "将载入后台最新配置与目标人格，丢弃本页尚未保存的设置和核心人设草稿。", submit: "丢弃并重载", danger: true })) return;
  await perform(async () => {
    const previous = selectedPersona;
    selectedPersona = "";
    try { await loadState({ replaceCore: true }); renderConfig({ replace: true }); }
    catch (error) { selectedPersona = previous; throw error; }
  }, "已载入后台最新配置。");
});
byId("learning-toggle").addEventListener("click", () => {
  const next = !paused();
  return perform(() => action("learning_pause", { paused: next }), next ? "已暂停自动学习。" : "已恢复自动学习。");
});

byId("persona-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (coreDirty && !await ask({ title: "切换人格", description: "当前核心人设有未保存修改。切换后将丢弃这份草稿。", submit: "丢弃并载入", danger: true })) return;
  await perform(async () => {
    const previous = selectedPersona;
    selectedPersona = byId("persona-id").value.trim();
    try { await loadState({ replaceCore: true }); } catch (error) { selectedPersona = previous; byId("persona-id").value = previous; throw error; }
  }, "已载入人格。");
});

byId("core-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const content = byId("core-content").value.trim();
  if (!content) { notice("核心人设内容不能为空。", "error"); return; }
  return perform(() => action("core_save", { content }, { replaceCore: true }), "核心人设已保存为新版本。");
});

byId("core-seed").addEventListener("click", async () => {
  if (coreDirty && !await ask({ title: "载入推荐核心", description: "这会替换编辑器里尚未保存的草稿，载入后仍需点击保存才会更新人格。", submit: "替换草稿", danger: true })) return;
  await perform(async () => {
    const result = await bridge.apiGet("seed");
    if (typeof result?.content !== "string" || !result.content.trim()) throw new Error("推荐核心内容暂不可用。");
    byId("core-content").value = result.content;
    coreDirty = true;
    renderCoreDirty();
  }, "推荐核心已载入编辑器。检查内容后点击保存，才会更新人格。");
});

byId("core-rollback").addEventListener("click", async () => {
  const versionId = byId("core-versions").value;
  if (!versionId) return;
  const confirmed = await ask({ title: "恢复核心人设", description: "将使用预览中的版本替换当前核心人设。未保存的编辑内容会被丢弃。", submit: "恢复此版本", danger: true });
  if (confirmed) await perform(() => action("core_rollback", { version_id: versionId }, { replaceCore: true }), "已恢复所选核心人设版本。");
});

byId("user-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const userId = byId("user-id").value.trim();
  if (!userId) { notice("请输入需要检索的 QQ 号。", "error"); return; }
  return perform(async () => {
    const previous = selectedUser;
    selectedUser = userId;
    try { await loadState(); } catch (error) { selectedUser = previous; byId("user-id").value = previous; throw error; }
  }, "已载入该用户的记忆。");
});

byId("user-forget").addEventListener("click", async () => {
  if (!selectedUser) return;
  const confirmed = await ask({ title: "删除用户记忆", description: `将删除当前人格下用户 ${selectedUser} 的记忆。请确认 QQ 号，这项操作不能从本页面撤销。`, submit: "删除用户记忆", danger: true, confirmText: selectedUser });
  if (confirmed) await perform(() => action("user_forget", { user_id: selectedUser }), "已删除该用户的记忆。");
});

byId("scope-form").addEventListener("submit", (event) => {
  event.preventDefault();
  return perform(async () => {
    const previous = selectedScope;
    selectedScope = byId("scope-key").value;
    try { await loadState(); } catch (error) { selectedScope = previous; byId("scope-key").value = previous; throw error; }
  }, "已载入群表达。");
});

byId("style-rollback").addEventListener("click", async () => {
  const versionId = byId("style-versions").value;
  if (!versionId || !selectedScope) return;
  const confirmed = await ask({ title: "恢复群表达", description: "当前群将恢复为预览中的表达版本，其他群不受影响。", submit: "恢复此版本", danger: true });
  if (confirmed) await perform(() => action("style_rollback", { scope_key: selectedScope, version_id: versionId }), "已恢复所选群表达版本。");
});

window.addEventListener("beforeunload", (event) => {
  if (!coreDirty && !configDirty) return;
  event.preventDefault();
  event.returnValue = "";
});

async function start() {
  setAvailability();
  if (!bridge?.ready || !bridge?.apiGet || !bridge?.apiPost) {
    notice("请从 AstrBot Dashboard 的插件详情页打开此管理页。当前未连接管理接口。", "error");
    byId("learning-status").textContent = "未连接";
    return;
  }
  try {
    await bridge.ready();
    ready = true;
    await perform(() => loadState({ replaceCore: true }));
  } catch (error) {
    notice(error?.message || "连接 Dashboard 失败。", "error");
    byId("learning-status").textContent = "连接失败";
  }
}

start();
