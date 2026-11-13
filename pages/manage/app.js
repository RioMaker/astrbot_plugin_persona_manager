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
let editingExpression = null;
let configBase = {};
let configSchema = {};
let configRevision = "";
let configDirty = false;
let configConflict = false;
let catalogOffset = 0;
let editingClassifier = null;
let editingMemory = null;
let catalogFilter = {};
const configControls = new Map();
const configSliders = new Map();
const settingsPanels = new Map();
let selectedSettingsPanel = "rhythm";

const array = (value) => (Array.isArray(value) ? value : []);
const object = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
const string = (value) => (value == null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2));
const first = (...values) => values.find((value) => value !== undefined && value !== null && value !== "");
const recordId = (record) => string(first(record.id, record.memory_id, record.style_id, record.version_id));
const recordText = (record) => string(first(record.content, record.text, record.summary, record.expression, record.pattern, record.prompt, record.core));
const memoryStates = { active: "当前有效", asserted: "已陈述", planned: "计划中", in_progress: "进行中", completed: "已完成", cancelled: "已取消" };

const categoryLabels = {timed:"短期 · 时效性", daily:"短期 · 日常（下次入梦清理）", cache:"短期 · 有限缓存", relationship:"长期 · 关系", event:"长期 · 情绪事件", knowledge:"长期 · 知识", self:"长期 · 自我认知"};
const provenanceLabels = {self_report:"本人陈述", hearsay:"他人转述", inferred:"主观推测", self_reflection:"自我反思", admin:"管理员确认"};
const accuracyLabels = {unconfirmed:"未核实", supported:"有来源支持", confirmed:"已核实"};
const archiveStatusLabels = {active:"生效中",pending:"暂存待确认",expired:"已到期",rejected:"已拒绝",superseded:"旧认知",corrected:"已纠正"};

function fillOptions(control, options, value) {
  control.replaceChildren();
  Object.entries(options).forEach(([key,label]) => { const option=node("option",label); option.value=key; control.append(option); });
  control.value = value;
}

function renderArchive() {
  const classes = [...array(state.classifiers)].sort((a,b)=>(a.status === "confirmed")-(b.status === "confirmed"));
  const target = byId("classifiers"); target.replaceChildren(); target.className="classifier-grid";
  const pending = classes.filter(c=>c.status !== "confirmed").length;
  target.append(node("p", `${pending} 个类型待核对，其中长期类型须确认后使用。`, "muted classifier-caption"));
  for (const cls of classes) {
    const item=node("article","",`classifier-row ${cls.status !== "confirmed" ? "classifier-pending" : ""}`);
    const status={confirmed:"已确认",auto:"短期已自动通过 · 待核对",pending:"长期待确认 · 仅暂存",rejected:"已拒绝"}[cls.status] || cls.status;
    item.append(node("strong",cls.name),node("span",`${categoryLabels[cls.category]} · ${status}`,"tag"),node("p",cls.description,"muted"));
    item.append(node("p",array(cls.keywords).join(" · "),"small"),mutationButton("编辑 / 确认分类",()=>editClassifier(cls)));
    target.append(item);
  }
  const classDraft=byId("catalog-class").value;
  fillOptions(byId("catalog-class"), {"":"全部类型",...Object.fromEntries(classes.map(c=>[c.id,c.name]))}, classDraft || catalogFilter.catalog_class || "");
  const budget=object(state.memory_usage), values=object(state.config?.values);
  const daily=Number(budget.daily?.mtokens || 0), monthly=Number(budget.monthly?.mtokens || 0);
  byId("memory-budget").replaceChildren(node("h3",state.schedule?.state === "thinking" ? "正在思考并整理记忆" : "整理用量与入梦"),node("p",`今日 ${daily.toFixed(6)} / ${values.memory_daily_mtokens ?? "—"} Mtokens · 本月 ${monthly.toFixed(6)} / ${values.memory_monthly_mtokens ?? "—"} Mtokens`),node("p",`1 Mtokens = 100万 tokens；包含在途预留与未知用量估算。最近入梦：${dateText(state.last_dream)}`,"muted small"));
  const catalog=object(state.memory_catalog), items=array(catalog.items);
  byId("catalog-count").textContent=`共 ${catalog.total || 0} 条 · 当前 ${catalogOffset + (items.length ? 1 : 0)}–${catalogOffset + items.length}`;
  const list=byId("catalog-items"); list.replaceChildren();
  if (!items.length) list.append(node("p","暂无符合条件的记忆。","empty"));
  for (const memory of items) {
    const card=node("article","","card memory-card");
    const owner=memory.user_id === "__self__" ? "自我认知" : memory.user_id === "__knowledge__" ? "知识库" : `用户 ${memory.user_id}`;
    card.append(node("h3",`${memory.key || "已清理内容"} · ${owner}`),node("p",`${memory.class_name || categoryLabels[memory.category]} · ${archiveStatusLabels[memory.status] || memory.status} · ${provenanceLabels[memory.provenance] || memory.provenance}`,"tag"),node("p",memory.content,"item-content"));
    card.append(node("p",`准确性：${accuracyLabels[memory.accuracy] || memory.accuracy} · 置信度 ${memory.confidence} · 核实 ${memory.confirmations || 0} 次 · 心情：${memory.mood || "未记录"}`,"muted"));
    if (memory.expires_at) card.append(node("p",`有效至 ${dateText(memory.expires_at)}`,"small"));
    if (memory.hold_reason) card.append(node("p", {classification:"待管理员确认分类，不参与聊天召回。",knowledge:"知识证据尚未达到门槛，不参与聊天召回。",correction:"纠错提案待核对，原记忆继续有效；确认时会再次检查原记录版本。"}[memory.hold_reason] || "待管理员核对，不参与聊天召回。","field-hint"));
    card.append(detailsFor(memory));
    const actions=node("div","","item-actions");
    if (memory.status === "pending" && memory.hold_reason === "correction") {
      actions.append(mutationButton("确认纠错", async()=>{
        if (await ask({title:"确认纠错提案",description:memory.content,submit:"确认并替换原记忆"})) await perform(()=>action("memory_approve",{memory_id:memory.id,user_id:memory.user_id,revision:memory.revision}),"纠错已确认。");
      }), mutationButton("拒绝纠错",()=>perform(()=>action("memory_reject",{memory_id:memory.id,user_id:memory.user_id,revision:memory.revision}),"纠错已拒绝。")));
    }
    if (["active","pending","rejected"].includes(memory.status)) actions.append(mutationButton("编辑全部细节",()=>editMemoryDetails(memory)));
    actions.append(mutationButton("忘记", async()=>{
      if (await ask({title:"忘记记忆",description:memory.content,danger:true})) await perform(()=>action("memory_forget",{memory_id:memory.id,user_id:memory.user_id}),"记忆已遗忘。");
    },{danger:true}));
    card.append(actions); list.append(card);
  }
}

function editClassifier(cls) {
  if (busy || !ready) return;
  editingClassifier=cls;
  byId("classifier-name").value=cls.name;
  fillOptions(byId("classifier-category"),categoryLabels,cls.category);
  byId("classifier-description").value=cls.description;
  byId("classifier-tags").value=array(cls.keywords).join("，");
  byId("classifier-ttl").value=cls.ttl_seconds;
  byId("classifier-confirm").checked=cls.status === "confirmed";
  byId("classifier-error").textContent="";
  byId("classifier-dialog").showModal();
}

function editMemoryDetails(memory) {
  if (busy || !ready) return;
  editingMemory=memory;
  const target=byId("memory-detail-fields"); target.replaceChildren();
  const classes=Object.fromEntries(array(state.classifiers).map(c=>[c.id,c.name]));
  const fields=[
    ["class_id","记忆分类",classes], ["key","主题 / 独立特性"], ["content","记忆正文","textarea"],
    ["provenance","信息来源",provenanceLabels], ["accuracy","准确性",accuracyLabels],
    ["confidence","置信度（0～1）","number"], ["mood","当时我的心情"], ["salience","情绪波动（0～1）","number"],
    ["ttl_seconds","时效记忆有效期（秒）","number"], ["tags","关键词标签（逗号分隔）"],
    ["relationship","我认为的关系"], ["address","称呼"], ["person_info","人物信息","textarea"], ["utterance","对我说过的话","textarea"]
  ];
  for (const [key,label,type] of fields) {
    const wrap=node("div","","field"), name=node("label",label); name.htmlFor=`detail-${key}`;
    const control=node(typeof type === "object" ? "select" : type === "textarea" ? "textarea" : "input"); control.id=`detail-${key}`;
    let value=key === "tags" ? array(memory.tags).join("，") : memory[key] ?? memory.details?.[key] ?? "";
    if (typeof type === "object") fillOptions(control,type,value);
    else {control.value=value; if (type === "number") { control.type="number"; control.step=key === "ttl_seconds" ? "1" : "0.01"; control.min=key === "ttl_seconds" ? "1" : "0"; control.max=key === "ttl_seconds" ? "2592000" : "1";} else control.maxLength=600;}
    wrap.append(name,control); target.append(wrap);
  }
  byId("memory-detail-error").textContent=""; byId("memory-detail-dialog").showModal();
}

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
  Object.assign(params, catalogFilter, {catalog_offset: catalogOffset});
  const result = await bridge.apiGet("state", params);
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("管理接口返回了无法识别的数据。");
  state = result;
  selectedPersona = string(first(state.persona?.id, state.persona?.persona_id, selectedPersona));
  byId("persona-id").value = selectedPersona;
  renderState(replaceCore);
}

async function action(actionName, payload = {}, { replaceCore = false } = {}) {
  const result = await bridge.apiPost("action", { action: actionName, persona_id: selectedPersona, user_id: selectedUser, scope_key: selectedScope, ...payload });
  if (result?.saved !== true) throw new Error("管理接口未确认保存，请刷新核对结果。");
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
  byId("learning-status").textContent = disabledByConfig ? "配置已关闭学习" : isPaused ? "学习已暂停" : state.schedule?.state === "thinking" ? "正在思考 · 整理记忆" : "学习已开启";
  byId("learning-status").className = `status-pill ${isPaused || disabledByConfig ? "paused" : "running"}`;
  byId("learning-toggle").textContent = isPaused ? "恢复学习" : "暂停学习";
  byId("status-detail").textContent = string(status);
  byId("persona-caption").textContent = selectedPersona ? `正在管理：${selectedPersona}` : "后端未返回人格 ID，请检查插件配置。";
  // Keep memory diagnostics visible even if a later, unrelated panel fails.
  renderOverview();
  renderArchive();
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
}

function renderCoreDirty() {
  const length = byId("core-content").value.length;
  byId("core-dirty").textContent = `${length.toLocaleString("zh-CN")} 字 · ${coreDirty ? "有未保存修改" : "已与当前版本同步"}`;
}

const settingsLayout = [
  { id: "rhythm", title: "整理节奏", caption: "什么时候开始思考", description: "按群积累已回复的对话，达到门槛后批量整理；仍受时段、间隔与预算约束。", fields: ["enabled", "learning_enabled", "persona_id", "memory_enabled", "memory_batch_threshold", "batch_size", "thinking_notice"] },
  { id: "lifecycle", title: "记忆生命周期", caption: "七类记忆如何留存", description: "短期按时间、入梦或容量清理；长期保留来源、证据与审核状态。", fields: ["short_cache_capacity", "auto_dream", "dream_hour", "knowledge_confirmations", "event_salience_threshold"] },
  { id: "budget", title: "模型与预算", caption: "模型选择与用量上限", description: "记忆整理按输入与输出合计记账。1 Mtokens = 100 万 tokens；任一相关限额为 0 时停止模型整理。", fields: ["memory_daily_mtokens", "memory_monthly_mtokens", "learning_provider_id", "model_timeout_seconds", "max_output_tokens", "daily_calls", "daily_group_calls", "daily_token_limit", "daily_group_token_limit"] },
  { id: "groups", title: "群与共享", caption: "哪些群可以学习、读取", description: "门禁负责放行；此处控制记忆与表达权限。每位用户统一存储记忆，各群是否读取公共记忆单独设置。", fields: ["memory_default_enabled", "observation_groups", "memory_groups", "memory_disabled_groups", "public_memory_groups", "global_style_groups", "group_tags", "ignored_qq"] },
  { id: "expression", title: "表达与方向", caption: "怎么回忆、怎样表达", description: "调整聊天时的记忆与表达用量，设置成长方向；固定核心人设仍单独管理。", fields: ["evolution_guidance", "expression_per_turn", "expression_cooldown_seconds", "memory_context_chars", "expression_context_chars"] },
  { id: "advanced", title: "高级选项", caption: "学习时段与原文保留", description: "旁听积累、学习时段、原文长度与保留时间。互动记忆的数量门槛在“整理节奏”中设置。", fields: ["price_aware_learning", "learning_windows", "cheap_learning_windows", "min_observation_batch", "cheap_observation_batch", "learning_interval_seconds", "cheap_learning_interval_seconds", "max_learning_wait_seconds", "max_source_chars", "max_batch_chars", "max_context_chars", "observation_retention_hours", "interaction_retention_days", "regular_interaction_batch", "cheap_interaction_batch"] },
];
const settingsRanges = {
  memory_batch_threshold: [1, 60, 1, "条"], short_cache_capacity: [1, 1000, 1, "条 / 用户"],
  knowledge_confirmations: [2, 20, 1, "次独立核实"], event_salience_threshold: [0, 1, 0.01, "情绪强度"],
  dream_hour: [0, 23, 1, "时 · 北京时间"],
};
const settingsUnits = { memory_daily_mtokens: "Mtokens / 日", memory_monthly_mtokens: "Mtokens / 月", batch_size: "条 / 批" };
const optionNames = { immediate: "积累够样本即学习", adaptive: "自动判断", windows: "仅指定时段", hybrid: "组合策略" };

function settingsPanelFor(name) {
  return settingsLayout.find((panel) => panel.fields.includes(name))?.id || "advanced";
}

function selectSettingsPanel(id) {
  selectedSettingsPanel = settingsPanels.has(id) ? id : "rhythm";
  for (const [key, panel] of settingsPanels) {
    const active = key === selectedSettingsPanel;
    panel.section.hidden = !active;
    panel.button.classList.toggle("active", active);
    if (active) panel.button.setAttribute("aria-current", "true");
    else panel.button.removeAttribute("aria-current");
  }
}

function draftNumber(name) {
  const value = configControls.get(name)?.value;
  if (value === undefined || !String(value).trim()) return null;
  const number = Number(value), schema = configSchema[name] || {};
  if (!Number.isFinite(number) || number < (schema.minimum ?? settingsRanges[name]?.[0] ?? 0) || number > (schema.maximum ?? settingsRanges[name]?.[1] ?? Infinity) || (schema.type === "int" && !Number.isInteger(number))) return null;
  return number;
}

function draftDisplay(name, suffix = "") {
  const value = draftNumber(name);
  return value === null ? "待填写有效值" : `${value}${suffix}`;
}

function renderSettingsBudget() {
  const target = byId("settings-budget-preview");
  target.replaceChildren();
  for (const [period, label, field] of [["daily", "今日整理用量", "memory_daily_mtokens"], ["monthly", "本月整理用量", "memory_monthly_mtokens"]]) {
    const rawUsed = state?.memory_usage?.[period]?.mtokens;
    const used = typeof rawUsed === "number" && Number.isFinite(rawUsed) && rawUsed >= 0 ? rawUsed : null;
    const limit = draftNumber(field), ratio = used !== null && limit > 0 ? Math.min(100, used / limit * 100) : 0;
    const exhausted = used !== null && limit > 0 && used >= limit;
    const card = node("article", "", `budget-preview-card${exhausted || limit === 0 ? " budget-limited" : ""}`);
    card.id = `settings-budget-${period}`;
    card.append(node("span", label, "metric-label"), node("strong", used === null ? "—" : used.toFixed(6), "budget-number"), node("span", ` / ${limit ?? "待填写"} Mtokens`, "muted small"));
    const track = node("div", "", "budget-track");
    track.setAttribute("role", "progressbar"); track.setAttribute("aria-label", label);
    track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100");
    if (used !== null && limit > 0) track.setAttribute("aria-valuenow", String(Math.round(ratio)));
    const fill = node("div", "", "budget-fill"); fill.style.width = `${ratio}%`; track.append(fill);
    const note = limit === null ? "请填写有效限额" : limit === 0 ? "限额为 0 · 模型整理停用" : used === null ? "暂未取得用量数据" : exhausted ? "已达到此限额" : `剩余 ${(limit - used).toFixed(6)} Mtokens`;
    track.setAttribute("aria-valuetext", note);
    card.append(track, node("p", note, "small")); target.append(card);
  }
}

function renderSettingsPreview() {
  if (!configControls.size) return;
  byId("settings-preview-state").textContent = configDirty ? "草稿预览 · 尚未生效" : "已保存配置";
  const flow = byId("settings-flow"); flow.replaceChildren();
  const pending = array(state?.classifiers).filter((cls) => cls.status === "pending").length;
  const auto = array(state?.classifiers).filter((cls) => cls.status === "auto").length;
  const steps = [
    ["01", "积累对话", draftDisplay("memory_batch_threshold", " 条后整理"), "每群独立积累", "rhythm"],
    ["02", "思考整理", "批量提炼记忆", configControls.get("thinking_notice")?.checked ? "开启来源群思考提示" : "仅控制台显示思考状态", "rhythm"],
    ["03", "按属性留存", "3 类短期 · 4 类长期", "到期、入梦、容量淘汰", "lifecycle"],
    ["04", "确认新分类", pending ? `${pending} 类长期待确认` : "长期确认后使用", `短期自动通过 · ${auto} 类待核对`, "archive"],
  ];
  for (const [index, label, value, hint, destination] of steps) {
    const step = node("button", "", "memory-flow-step"); step.type = "button";
    step.append(node("span", `${index} / ${label}`, "flow-label"), node("strong", value), node("span", hint, "small muted"));
    step.addEventListener("click", () => destination === "archive" ? showPanel("archive") : selectSettingsPanel(destination));
    flow.append(step);
  }
  const off = ["enabled", "learning_enabled", "memory_enabled"].some((name) => configControls.get(name)?.checked === false);
  byId("settings-flow-note").textContent = off ? "当前草稿已关闭记忆整理相关开关；保存后不再进行模型记忆整理。" : paused() ? "运行中的学习已暂停，可通过页面顶部“恢复学习”继续。参数预览仍可编辑。" : "点击流程卡片跳转设置；时段、间隔和预算共同决定实际执行时间。";
  const daily = configControls.get("auto_dream")?.checked;
  const hour = draftNumber("dream_hour");
  const dream = daily ? hour === null ? "入梦时间待填写" : `每日 ${String(hour).padStart(2, "0")}:00 入梦清理` : "下次手动入梦时清理";
  const cards = [
    ["timed", "时效性记忆", "各条记忆独立计时", "如 5 分钟、30 分钟；到期即停止召回。默认时长在分类器中调整。"],
    ["daily", "日常记忆", dream, "保留到下一次入梦，按北京时间执行。关闭自动入梦后仍可手动入梦。"],
    ["cache", "缓存短期记忆", draftDisplay("short_cache_capacity", " 条 / 用户"), "容量满后先移除最旧一条；跨群合计容量，读取仍受群权限约束。"],
    ["relationship", "关系记忆", "来源与准确性分开记录", "主观关系、称呼、人物信息、对我说过的话和当时心情。"],
    ["event", "事件记忆", `情绪强度 ≥ ${draftDisplay("event_salience_threshold")}`, "需要实际回复证据与明确心情，保留情绪波动显著的经历。"],
    ["knowledge", "知识记忆", `${draftDisplay("knowledge_confirmations")} 次独立核实`, "证据不足先暂存；管理员确认可直接录入，重复引用不增加次数。"],
    ["self", "自我认知", "按独立特性更新", "从实际言语行为提炼自我认识，保留旧版本，不改写核心身份。"],
  ];
  const short = byId("settings-short-types"), long = byId("settings-long-types");
  short.replaceChildren(); long.replaceChildren();
  cards.forEach(([category, title, value, description], index) => {
    const card = node("article", "", `memory-type-card ${index < 3 ? "short-type" : "long-type"}`);
    card.id = `settings-type-${category}`;
    card.append(node("h4", title), node("strong", value), node("p", description));
    (index < 3 ? short : long).append(card);
  });
  byId("settings-classifier-note").textContent = `${pending} 类长期待确认 · ${auto} 类短期已自动通过、待核对。修改分类会同步迁移该类已有缓存。`;
  byId("settings-dream-status").textContent = `当前人格：${selectedPersona} · 最近入梦：${state.last_dream == null ? "尚无记录" : dateText(state.last_dream)}`;
  byId("settings-sharing-note").textContent = configControls.get("memory_default_enabled")?.checked
    ? "门禁放行 → 默认记录互动记忆 → 本群读取；开启“公共记忆”后，才可读取其他群的用户记忆与知识库。"
    : "门禁放行 → 仅所选群记录互动记忆 → 本群读取；公共记忆仍需逐群开启。";
  for (const [name, slider] of configSliders) {
    const value = draftNumber(name);
    if (value !== null) slider.value = String(value);
  }
  for (const [id, panel] of settingsPanels) {
    const changed = [...configControls].filter(([name, control]) => settingsPanelFor(name) === id && (configSchema[name].type === "bool" ? control.checked !== configBase[name] : control.value !== configValueText(configBase[name], configSchema[name]))).length;
    panel.badge.textContent = changed ? `${changed} 项修改` : "";
    panel.badge.hidden = !changed;
  }
  renderSettingsBudget();
}

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
  renderSettingsPreview();
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
    memory: (configControls.get("memory_default_enabled")?.checked || groupMatches("memory_groups", group)) && !groupMatches("memory_disabled_groups", group),
    global: groupMatches("global_style_groups", group),
    public: groupMatches("public_memory_groups", group),
  });
  const selectedCount = groups.filter((group) => Object.values(flags(group)).some(Boolean)).length;
  byId("group-count").textContent = `${groups.length} 个群 · 已选 ${selectedCount}`;
  const globalDisabled = configControls.get("enabled")?.checked === false || configControls.get("learning_enabled")?.checked === false || paused();
  byId("group-learning-note").textContent = globalDisabled
    ? "当前总开关关闭或学习已暂停。可先选择群，恢复学习后才会收集新样本。"
    : configControls.get("memory_enabled")?.checked === false
      ? "互动记忆总开关已关闭，已选的互动记忆群暂不生效。"
      : "旁听与互动控制采集。跨群表达独立控制跨群使用：不启用采集，也不开放白名单；按本群标签筛选同人格已启用表达。本群表达优先。";
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
    for (const [kind, field, labelText] of [["observation", "observation_groups", "旁听学表达"], ["memory", "memory_groups", "互动记忆"], ["global", "global_style_groups", "允许使用跨群表达"], ["public", "public_memory_groups", "允许读取公共记忆"]]) {
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
        if (kind === "memory") setGroupEntry("memory_disabled_groups", group, !checkbox.checked);
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
  configSliders.clear();
  settingsPanels.clear();
  const groupPicker = byId("settings-group-picker");
  const target = byId("settings-sections");
  target.replaceChildren();
  const nav = byId("settings-nav");
  nav.replaceChildren();
  const sections = new Map();
  const extraFields = new Map();
  for (const [index, layout] of settingsLayout.entries()) {
    const section = node("section", "", "card settings-section");
    section.id = `config-section-${layout.id}`;
    section.setAttribute("aria-labelledby", `config-title-${layout.id}`);
    const title = node("h3", layout.title); title.id = `config-title-${layout.id}`;
    const head = node("div", "", "settings-section-head");
    head.append(node("span", `0${index + 1}`, "section-number"), title, node("p", layout.description, "muted small"));
    section.append(head);
    if (layout.id === "lifecycle") {
      const dream = node("div", "", "settings-callout");
      const dreamCopy = node("div"), dreamStatus = node("p"); dreamStatus.id = "settings-dream-status";
      dreamCopy.append(node("strong", "手动入梦"), dreamStatus, node("p", "立即清理日常记忆；关闭定时入梦时也可使用，不会提交未保存的设置。"));
      const dreamButton = mutationButton("立即入梦", requestDream, { danger: true }); dreamButton.id = "settings-dream-now";
      dream.append(dreamCopy, dreamButton); section.append(dream);
      const types = node("div", "", "memory-types");
      for (const [id, label, caption] of [["short", "短期记忆", "有边界地留存"], ["long", "长期记忆", "按证据与确认留存"]]) {
        const lane = node("div", "", "memory-lane"), list = node("div", "", "memory-type-grid"); list.id = `settings-${id}-types`;
        const heading = node("div", "", "memory-lane-heading"); heading.append(node("h4", label), node("span", caption, "muted small"));
        lane.append(heading, list); types.append(lane);
      }
      const review = node("div", "", "settings-callout");
      const note = node("p"); note.id = "settings-classifier-note";
      const button = node("button", "管理分类与有效期 →"); button.type = "button"; button.addEventListener("click", () => showPanel("archive"));
      review.append(note, button);
      section.append(types, review, node("h4", "调整留存与确认规则", "settings-subtitle"));
    }
    if (layout.id === "budget") {
      const preview = node("div", "", "settings-budget-preview"); preview.id = "settings-budget-preview";
      section.append(preview, node("p", "用量包含在途预留与未知用量估算；下方限额采用当前草稿。每日 / 每月按北京时间重置。", "muted small"));
    }
    if (layout.id === "groups") {
      const note = node("p", "", "sharing-route"); note.id = "settings-sharing-note";
      section.append(note);
    }
    const fields = node("div", "", "settings-fields");
    sections.set(layout.id, fields); section.append(fields);
    if (layout.id === "groups") { groupPicker.hidden = false; section.append(groupPicker); }
    if (["budget", "groups", "advanced"].includes(layout.id)) {
      const details = node("details", "", "settings-extra"); details.id = `settings-extra-${layout.id}`;
      const labels = { budget: "共同生效的调用次数与单群预算", groups: "高级：手动群范围与排除用户", advanced: "旧版兼容参数" };
      const description = { budget: "以下上限由旁听与互动整理共用。记忆日/月预算有剩余时，也可能因这些上限暂停。", groups: "与上方群开关同步。可手动添加尚未发现的群，未知群号不会因筛选而丢失。", advanced: "保留旧版配置兼容；当前互动整理使用“整理节奏”中的数量门槛，这两项不再控制它。" };
      const extra = node("div", "", "settings-fields");
      details.append(node("summary", labels[layout.id]), node("p", description[layout.id], "muted small"), extra);
      extraFields.set(layout.id, extra); section.append(details);
    }
    const button = node("button", "", "settings-nav-button"); button.type = "button";
    button.id = `settings-nav-${layout.id}`; button.setAttribute("aria-controls", section.id);
    const copy = node("span", "", "settings-nav-copy"), badge = node("span", "", "settings-nav-badge");
    copy.append(node("strong", layout.title), node("small", layout.caption));
    button.append(node("span", `0${index + 1}`, "settings-nav-number"), copy, badge);
    button.addEventListener("click", () => selectSettingsPanel(layout.id));
    nav.append(button); target.append(section);
    settingsPanels.set(layout.id, { section, button, badge });
  }
  const fieldsByName = new Map();
  for (const [name, schemaValue] of Object.entries(configSchema)) {
    const schema = object(schemaValue);
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
      control.type = ["int", "float"].includes(schema.type) ? "number" : "text";
      control.value = configValueText(configBase[name], schema);
      if (["int", "float"].includes(schema.type)) {
        control.step = schema.type === "float" ? "any" : "1";
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
    if (schema.type === "bool") control.setAttribute("role", "switch");
    control.addEventListener(schema.type === "bool" || control.tagName === "SELECT" ? "change" : "input", updateConfigDirty);
    field.append(label);
    const unit = settingsRanges[name]?.[3] || settingsUnits[name];
    if (unit) {
      const wrapper = node("div", "", "number-with-unit"); wrapper.append(control, node("span", unit)); field.append(wrapper);
    } else field.append(control);
    if (settingsRanges[name]) {
      const [min, max, step] = settingsRanges[name];
      const slider = node("input"); slider.type = "range"; slider.id = `range-${name}`; slider.className = "config-range";
      slider.min = schema.minimum ?? min; slider.max = schema.maximum ?? max; slider.step = step; slider.value = control.value;
      slider.setAttribute("aria-label", `${schema.description || name}（滑动调节）`);
      slider.addEventListener("input", () => { control.value = slider.value; updateConfigDirty({ renderGroups: false }); });
      configSliders.set(name, slider); field.append(slider);
    }
    if (schema.hint) {
      const hint = node("p", schema.hint, "field-hint"); hint.id = `hint-${name}`;
      control.setAttribute("aria-describedby", hint.id); field.append(hint);
    }
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
    fieldsByName.set(name, field);
  }
  const folded = new Set(["daily_calls", "daily_group_calls", "daily_token_limit", "daily_group_token_limit", "observation_groups", "memory_groups", "memory_disabled_groups", "global_style_groups", "group_tags", "public_memory_groups", "ignored_qq", "regular_interaction_batch", "cheap_interaction_batch"]);
  for (const name of [...settingsLayout.flatMap((panel) => panel.fields), ...fieldsByName.keys()]) {
    const field = fieldsByName.get(name);
    if (!field) continue;
    const panel = settingsPanelFor(name);
    (folded.has(name) ? extraFields.get(panel) : sections.get(panel)).append(field);
    fieldsByName.delete(name);
  }
  selectSettingsPanel(selectedSettingsPanel);
  byId("settings-count").textContent = `${configControls.size} 项设置`;
  updateConfigDirty();
}

function configInputError(name, message) {
  selectSettingsPanel(settingsPanelFor(name));
  const control = configControls.get(name);
  const details = control?.closest?.("details");
  if (details) details.open = true;
  control?.focus();
  return new Error(message);
}

function changedConfigValues() {
  const changes = {};
  for (const [name, control] of configControls) {
    const schema = configSchema[name];
    let value;
    if (schema.type === "bool") value = control.checked;
    else if (schema.type === "int") {
      if (!/^-?\d+$/.test(control.value.trim())) throw configInputError(name, `${schema.description || name}：请填写整数。`);
      value = Number(control.value);
      if (!Number.isSafeInteger(value) || (schema.minimum !== undefined && value < schema.minimum) || (schema.maximum !== undefined && value > schema.maximum)) throw configInputError(name, `${schema.description || name}：超出允许范围 ${schema.minimum ?? "不限"}～${schema.maximum ?? "不限"}。`);
    } else if (schema.type === "float") {
      value = Number(control.value);
      if (!control.value.trim() || !Number.isFinite(value) || value < 0 || value > (schema.maximum ?? 10000)) throw configInputError(name, `${schema.description}：请填写0～${schema.maximum ?? 10000}的数字。`);
    } else if (schema.type === "list") value = [...new Set(control.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
    else value = control.value.trim();
    if (name === "evolution_guidance" && value.length > 4000) throw configInputError(name, "进化方向最多 4000 字。");
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
const scheduleNames = { idle: "等待样本", disabled: "已关闭", paused: "已暂停", learning: "学习中", running: "学习中", thinking: "思考整理中", collecting: "积累对话", missing_provider: "缺少学习模型", cooldown: "间隔等待", budget: "预算不足", budget_exhausted: "预算不足", outside_window: "允许时段外", waiting_samples: "积累样本", insufficient_samples: "积累样本", insufficient: "积累样本", cheap: "低价时段", regular: "常规时段", blocked: "权限受限", deferred: "暂缓学习", complete: "学习完成", completed: "学习完成", success: "学习完成", error: "检查失败", failed: "学习失败", cancelled: "已取消", discarded: "结果作废" };

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
  const status = object(state.status);
  const capture = object(status.capture);
  addMeta(details, "运行版本", `${status.runtime_version ? `v${status.runtime_version}` : "未知"} / 页面 v2.3.0`);
  addMeta(details, "已采集原文", countText(status.sources));
  addMeta(details, "待整理对话", countText(status.pending_interactions));
  addMeta(details, "生效记忆", countText(status.memories));
  addMeta(details, "待确认记忆", countText(status.pending_memories));
  const phase = first(schedule.phase, schedule.price_period, schedule.price_band);
  addMeta(details, "当前价段", phase === "cheap" ? "低价" : phase ? "常规" : undefined);
  addMeta(details, "最近采集检查", capture.reason || "暂无采集诊断；请在来源群正常对话后刷新，旧版本需先重载插件。");
  if (capture.group_id) addMeta(details, "采集检查群", capture.group_id);
  if (capture.checked_at) addMeta(details, "采集检查时间", dateText(capture.checked_at));
  if (capture.last_seen_at) addMeta(details, "最近收到群消息", dateText(capture.last_seen_at));
  if (capture.last_request_at) addMeta(details, "最近进入模型请求", dateText(capture.last_request_at));
  addMeta(details, "最近学习", status.last_learning_at ? dateText(status.last_learning_at) : "暂无");
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
  if (!rows.length) {
    const status = object(state.status);
    const cfg = object(state.config?.values);
    const capture = object(status.capture);
    const sources = counter(status.sources);
    const pending = counter(status.pending_interactions);
    const explanation = ["没有调用记录不等于没有采集。"];
    if (status.enabled === false || cfg.enabled === false) explanation.push("插件已关闭；开启后再检查群聊采集与整理。");
    else if (status.learning_enabled === false || cfg.learning_enabled === false) explanation.push("学习已关闭；开启后才会继续整理记忆。");
    else if (status.paused) explanation.push("学习已暂停；恢复后再检查待整理对话。");
    else if (cfg.memory_enabled === false) explanation.push("记忆系统已关闭，相处记忆不会自动整理。");
    else if (pending > 0) explanation.push(`${sources === null ? "" : `已采集 ${countText(sources)} 条原文，`}有 ${countText(pending)} 条对话待整理；满足门槛、时段、间隔和预算后才会调用模型。`);
    else if (sources === 0) explanation.push("尚未采集到原文；请让可可子在门禁放行的群里正常回复一条消息，再刷新查看采集状态。");
    else if (sources > 0) explanation.push(`已采集 ${countText(sources)} 条原文${pending === 0 ? "，当前没有待整理的相处对话" : "，待整理数量暂未取得"}。`);
    else explanation.push("采集数量暂未取得；后台开始模型整理后才会生成调用记录。");
    if (capture.reason) explanation.push(`最近采集检查：${capture.reason}`);
    if (state.schedule?.reason) explanation.push(`当前整理检查：${state.schedule.reason}`);
    target.append(node("p", explanation.join(" "), "empty"));
    return;
  }
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
    addMeta(meta, "用途", memory.pinned ? "固定偏好" : "按话题召回");
    addMeta(meta, "适用条件", array(memory.qualifiers).join("；"));
    if (memory.status === "pending") addMeta(meta, "审核", "待确认 · 不参与召回");
    if (memory.status === "rejected") addMeta(meta, "审核", "已拒绝 · 不参与召回");
    const actions = node("div", "", "item-actions");
    actions.append(mutationButton("纠正", () => correctMemory(memory), { available: Boolean(recordId(memory)) && memory.status === "active" }), mutationButton("忘记", () => forgetMemory(memory), { danger: true, available: Boolean(recordId(memory)) }));
    if (memory.status === "active") actions.append(mutationButton(memory.pinned ? "取消固定" : "设为固定偏好", () => perform(() => action(memory.pinned ? "memory_unpin" : "memory_pin", {memory_id: memory.id, revision: memory.revision}), "记忆用途已更新。")));
    if (memory.status === "pending") {
      actions.append(mutationButton("确认记忆", () => perform(() => action("memory_approve", {memory_id: memory.id, revision: memory.revision}), "记忆已确认。")), mutationButton("拒绝提案", () => perform(() => action("memory_reject", {memory_id: memory.id, revision: memory.revision}), "提案已拒绝。")));
    }
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
    addMeta(meta, "经验类型", ({phrase:"短表达", grammar:"句法习惯", response:"接话与收尾"})[style.form_type]);
    addMeta(meta, "确认发送的注入", style.injections);
    addMeta(meta, "短语出现在回复中", style.uses);
    addMeta(meta, "明确好评 / 负评", `${style.positive || 0} / ${style.negative || 0}`);
    addMeta(meta, "复审原因", style.review_reason);
    card.append(head, node("p", recordText(style), "item-content"), meta, detailsFor(style));
    const reviewActions = node("div", "", "item-actions");
    reviewActions.append(mutationButton("编辑经验", () => editExpression(style)));
    if (style.status !== "active") reviewActions.append(mutationButton("审核启用", () => perform(() => action("style_approve", {style_id: style.id, revision: style.revision}), "该经验已审核启用。")));
    card.append(reviewActions);
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
  const turns = array(state.dialogues);
  if (turns.length) {
    const history = node("section", "", "card");
    history.append(node("h3", "近期实际对话"), node("p", "这里只展示框架确认发送后的回复。未明确反馈表示未知；句法习惯是否实际采用不作自动推断。", "muted small"));
    for (const turn of turns) {
      const item = node("details");
      item.append(node("summary", `${dateText(turn.received_at)} · ${turn.user_id}`), node("p", `用户：${turn.user_text}`), node("p", `可可子：${turn.reply_text || "本轮没有可记录的文字"}`), node("p", `反馈：${({positive:"明确认可",negative:"明确纠正",positive_unattributed:"认可，未归因到具体经验",negative_unattributed:"负面评价，未归因到具体经验"})[turn.feedback] || "未知"}`, "muted small"));
      history.append(item);
    }
    target.append(history);
  }
}

function editExpression(style) {
  editingExpression = style;
  for (const [field, value] of Object.entries({kind:style.form_type, situation:style.situation || array(style.contexts).join("；"), pattern:style.expression, avoid:array(style.avoid).join("；"), intent:style.intent, keywords:array(style.keywords).join("，")})) byId(`expression-${field}`).value = value || "";
  byId("expression-error").hidden = true;
  byId("expression-dialog").showModal();
}

byId("classifier-cancel").addEventListener("click",()=>byId("classifier-dialog").close());
byId("memory-detail-cancel").addEventListener("click",()=>byId("memory-detail-dialog").close());
byId("classifier-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if (busy || !ready || !editingClassifier) return;
  await perform(async()=>{
    const definition={id:editingClassifier.id,name:byId("classifier-name").value,category:byId("classifier-category").value,description:byId("classifier-description").value,keywords:byId("classifier-tags").value.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean),ttl_seconds:Number(byId("classifier-ttl").value)};
    try {await action("classifier_save",{definition,revision:editingClassifier.revision,confirm:byId("classifier-confirm").checked}); byId("classifier-dialog").close();}
    catch(error){byId("classifier-error").textContent=error.message;throw error;}
  },"分类已保存，相关缓存已同步迁移。");
});
byId("memory-detail-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if (busy || !ready || !editingMemory) return;
  await perform(async()=>{
    const changes={details:{...object(editingMemory.details)}};
    for (const key of ["class_id","key","content","provenance","accuracy","mood"]) changes[key]=byId(`detail-${key}`).value;
    for (const key of ["confidence","salience","ttl_seconds"]) changes[key]=Number(byId(`detail-${key}`).value);
    changes.tags=byId("detail-tags").value.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean);
    for (const key of ["relationship","address","person_info","utterance"]) changes.details[key]=byId(`detail-${key}`).value;
    try {await action("memory_detail_save",{memory_id:editingMemory.id,revision:editingMemory.revision,changes}); byId("memory-detail-dialog").close();}
    catch(error){byId("memory-detail-error").textContent=error.message;throw error;}
  },"记忆细节已保存。");
});
byId("catalog-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); catalogOffset=0;
  const owner=byId("catalog-owner-type").value;
  catalogFilter={catalog_user:owner === "self" ? "__self__" : owner === "knowledge" ? "__knowledge__" : owner === "person" ? byId("catalog-user").value.trim() : "",catalog_class:byId("catalog-class").value,catalog_status:byId("catalog-status").value};
  await perform(()=>loadState());
});
byId("catalog-owner-type").addEventListener("change",()=>{byId("catalog-user-field").hidden=byId("catalog-owner-type").value !== "person";});
byId("catalog-prev").addEventListener("click",()=>perform(async()=>{catalogOffset=Math.max(0,catalogOffset-100);await loadState();}));
byId("catalog-next").addEventListener("click",()=>perform(async()=>{if(catalogOffset+100 < Number(state.memory_catalog?.total || 0))catalogOffset+=100;await loadState();}));
async function requestDream() {
  if (busy || !ready || dialogRequest) return;
  const persona = selectedPersona;
  if (await ask({ title: "立即入梦", description: `清理人格「${persona}」下所有群、所有用户的日常记忆，清理后无法从页面恢复。时效、缓存和长期记忆按各自规则保留。此操作不会保存配置草稿。`, submit: "入梦并清理", danger: true })) {
    await perform(() => action("memory_dream", { persona_id: persona }), "已入梦，日常记忆已清理。");
  }
}
byId("dream-now").addEventListener("click", requestDream);
byId("knowledge-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  await perform(async()=>{await action("knowledge_force",{key:byId("knowledge-key").value,content:byId("knowledge-content").value});byId("knowledge-key").value="";byId("knowledge-content").value="";},"管理员确认的知识已录入。");
});
if (typeof window.setInterval === "function") window.setInterval(()=>{
  if(ready && !busy && !coreDirty && !configDirty && !document.querySelector("dialog[open]")) perform(()=>loadState());
},10000);

byId("expression-cancel").addEventListener("click", () => byId("expression-dialog").close());
byId("expression-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !editingExpression) return;
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  busy = true;
  setAvailability();
  try {
    const form = Object.fromEntries(["kind", "situation", "pattern", "avoid", "intent"].map((field) => [field, byId(`expression-${field}`).value.trim()]));
    form.keywords = byId("expression-keywords").value.split(/[,，、]/).map((value) => value.trim()).filter(Boolean);
    await action("style_edit", {style_id:editingExpression.id, revision:editingExpression.revision, form});
    byId("expression-dialog").close();
    notice("经验已保存为待审核。审核启用后用于聊天。");
  } catch (error) {
    byId("expression-error").textContent = error.message || "保存失败";
    byId("expression-error").hidden = false;
  } finally {
    busy = false;
    if (submit) submit.disabled = false;
    setAvailability();
  }
});

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

function showPanel(name) {
  document.querySelectorAll("[data-panel]").forEach((tab) => {
    const active = tab.dataset.panel === name;
    tab.classList.toggle("active", active);
    if (active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    byId(`panel-${tab.dataset.panel}`).hidden = !active;
  });
}

document.querySelectorAll("[data-panel]").forEach((button) => {
  button.addEventListener("click", () => showPanel(button.dataset.panel));
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
