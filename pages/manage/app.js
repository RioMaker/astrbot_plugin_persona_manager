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
}

function renderCoreDirty() {
  const length = byId("core-content").value.length;
  byId("core-dirty").textContent = `${length.toLocaleString("zh-CN")} 字 · ${coreDirty ? "有未保存修改" : "已与当前版本同步"}`;
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
    head.append(node("strong", first(style.name, style.meaning, style.situation, style.context, "表达习惯")), node("span", disabled ? "已停用" : first(style.status, "使用中"), "tag"));
    const meta = node("div", "", "item-meta");
    addMeta(meta, "学习时间", dateText(first(style.created_at, style.learned_at)));
    addMeta(meta, "适用情境", first(style.contexts, style.situation, style.context));
    addMeta(meta, "避免用于", style.avoid);
    addMeta(meta, "使用次数", first(style.use_count, style.count));
    card.append(head, node("p", recordText(style), "item-content"), meta, detailsFor(style));
    if (!disabled) {
      const actions = node("div", "", "item-actions");
      actions.append(mutationButton("停用此表达", async () => {
        const confirmed = await ask({ title: "停用群表达", description: "这条表达将停止用于当前群。已有历史版本会保留。", submit: "停用", danger: true });
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
byId("refresh").addEventListener("click", () => perform(() => loadState(), coreDirty ? "已刷新，未保存的核心人设草稿已保留。" : "已刷新。"));
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
  if (!coreDirty) return;
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
