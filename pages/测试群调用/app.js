const byId = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
const format = new Intl.NumberFormat("zh-CN");
const dates = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const labels = {
  running: "进行中", done: "已完成", delivered: "已确认发送", test_failed: "测试模型失败",
  error: "失败", cancelled: "已取消", incomplete: "未确认完成", unconfirmed: "未确认发送",
};
let scope = "";
let turnId = "";
let revision = 0;
let timer;
let detailId = "";
let lastDetail = "";
let groupSignature = "";
let lastTurns = "";

function el(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function duration(ms) {
  return ms < 1000 ? `${format.format(ms)} ms` : `${(ms / 1000).toFixed(2)} 秒`;
}

function tokenText(value) {
  return value == null ? "未报告" : format.format(value);
}

function stateLabel(status) {
  return labels[status] || status;
}

function clearDetail() {
  detailId = "";
  lastDetail = "";
  byId("detail").replaceChildren();
}

function selectGroup() {
  scope = byId("group").value;
  turnId = "";
  lastTurns = "";
  byId("turns").replaceChildren();
  byId("model").textContent = "";
  clearDetail();
  void refresh();
}

function renderGroups(data) {
  const control = byId("group");
  const signature = JSON.stringify([data.groups, data.scope]);
  if (signature !== groupSignature) {
    groupSignature = signature;
    control.replaceChildren();
    for (const group of data.groups) {
      const option = el("option", `${group.name ? group.name + " · " : ""}${group.group}（${group.platform}）`);
      option.value = group.scope;
      control.append(option);
    }
    control.disabled = !data.groups.length;
    control.value = data.scope;
  }
  const group = data.groups.find((row) => row.scope === data.scope);
  byId("model").textContent = group ? `当前测试模型 · ${group.label}` : "";
}

function renderTurns(data) {
  const target = byId("turns");
  const signature = JSON.stringify([data.turns.map((t) => [t.id, t.status]), data.detail?.id, data.scope]);
  if (signature === lastTurns) return;
  lastTurns = signature;
  target.replaceChildren();
  if (!data.turns.length) {
    target.append(el("p", data.scope ? "等待这个群的第一条测试调用。" : "暂无测试群调用。", "hint"));
  }
  for (const turn of data.turns) {
    const button = el("button", "", "turn");
    button.type = "button";
    button.setAttribute("aria-current", String(turn.id === data.detail?.id));
    button.append(el("strong", turn.preview || "群消息"), el("span",
      `${dates.format(turn.started_at * 1000)} · ${stateLabel(turn.status)}`, "meta"));
    button.addEventListener("click", () => {
      turnId = turn.id;
      clearDetail();
      void refresh();
    });
    target.append(button);
  }
}

function block(title, text, fold = false) {
  const box = el(fold ? "details" : "div", "", "block");
  box.append(el(fold ? "summary" : "strong", title), el("pre", text));
  return box;
}

function showStep(step, index) {
  const row = el("details", "", "step");
  const label = step.kind === "tool" ? `执行工具 · ${step.label}` : step.label;
  const header = el("summary", `${index + 1}. ${label}`);
  const usage = step.usage;
  const tokens = usage
    ? `输入 ${tokenText(usage.input_tokens)} · 输出 ${tokenText(usage.output_tokens)} · 合计 ${tokenText(usage.total_tokens)} tokens`
    : step.kind === "tool" ? "工具内部 token 未报告" : "模型用量不适用";
  header.append(el("span", `${stateLabel(step.status)} · ${step.kind === "receipt" ? "回执时间点" : duration(step.elapsed_ms)} · ${tokens}`, "meta"));
  row.append(header);
  if (step.input) row.append(block(step.kind === "tool" ? "工具参数" : step.kind === "delivery" ? "框架最终回复" : "输入内容", step.input));
  if (step.prompt) row.append(block("发送给模型的提示词与消息", step.prompt, true));
  if (step.tools) row.append(block("发送给模型的工具定义", step.tools, true));
  if (step.partial) row.append(block("中断前已返回的内容", step.partial));
  if (step.output) row.append(block(step.kind === "tool" ? "工具返回" : step.kind === "delivery" || step.kind === "receipt" ? "已发送内容" : "输出内容", step.output));
  return row;
}

function renderDetail(turn) {
  const signature = JSON.stringify(turn);
  if (lastDetail === signature) return;
  lastDetail = signature;
  const target = byId("detail");
  // Keep expanded prompts and their scroll positions during live updates.
  const previous = detailId === turn?.id ? [...target.querySelectorAll("details")].map((d) => d.open) : [];
  const scroll = detailId === turn?.id ? [...target.querySelectorAll("pre")].map((p) => p.scrollTop) : [];
  detailId = turn?.id || "";
  target.replaceChildren();
  if (!turn) {
    target.append(el("p", "选择一条调用查看各环节。", "hint"));
    return;
  }
  const heading = el("div", "", "heading");
  heading.append(el("strong", turn.preview || "群消息"), el("time", dates.format(turn.started_at * 1000) + " 北京时间"));
  target.append(heading, el("p", turn.model, "meta"));
  const facts = el("div", "", "facts");
  const reported = !turn.calls ? "暂无模型调用"
    : turn.reported_calls === turn.calls ? `合计 ${tokenText(turn.reported_tokens)} tokens`
    : turn.reported_calls ? `已报告 ${tokenText(turn.reported_tokens)} tokens（${turn.reported_calls}/${turn.calls} 次）`
    : "模型用量未报告";
  facts.append(el("strong", stateLabel(turn.status)), el("span", `全程 ${duration(turn.elapsed_ms)}`),
    el("span", `${turn.calls} 次测试模型调用`), el("span", reported));
  target.append(facts);
  turn.steps.forEach((step, index) => target.append(showStep(step, index)));
  if (turn.omitted) target.append(el("p", `另有 ${turn.omitted} 个环节超出展示上限。`, "hint"));
  const firstModel = turn.steps.findIndex((s) => s.kind === "model");
  const firstModelStep = firstModel < 0 ? null : target.querySelectorAll(".step")[firstModel];
  target.querySelectorAll("details").forEach((d, i) => { d.open = previous[i] ?? (d === firstModelStep); });
  target.querySelectorAll("pre").forEach((p, i) => { if (scroll[i]) p.scrollTop = scroll[i]; });
}

function notice(text, error = false) {
  byId("notice").textContent = text;
  byId("notice").className = error ? "error" : "";
}

function schedule() {
  window.clearTimeout(timer);
  if (byId("auto").checked && !document.hidden) timer = window.setTimeout(refresh, 5000);
}

async function refresh() {
  const version = ++revision;
  window.clearTimeout(timer);
  try {
    const data = await bridge.apiGet("trials", {scope, turn: turnId});
    if (version !== revision) return;
    scope = data.scope;
    if (turnId && !data.detail) {
      turnId = "";
      if (data.turns.length) return refresh();
    }
    renderGroups(data);
    renderTurns(data);
    renderDetail(data.detail);
    notice(!data.groups.length ? "尚未配置测试群。请在人格管理的“设置与方向 → 群与共享”选择百炼测试模型并保存。" :
      !data.scope ? "所选群的测试已停用，请重新选择测试群。" : "");
  } catch (error) {
    if (version !== revision) return;
    clearDetail();
    groupSignature = lastTurns = "";
    byId("turns").replaceChildren();
    byId("group").replaceChildren();
    byId("group").disabled = true;
    byId("model").textContent = "";
    notice(error?.message || "读取测试群调用失败。", true);
  } finally {
    if (version === revision) schedule();
  }
}

async function start() {
  if (!bridge?.ready || !bridge.apiGet) {
    notice("请从 AstrBot 插件详情打开“测试群调用”。", true);
    return;
  }
  try {
    await bridge.ready();
  } catch (error) {
    notice(error?.message || "连接 Dashboard 失败。", true);
    return;
  }
  byId("refresh").disabled = false;
  byId("refresh").addEventListener("click", refresh);
  byId("group").addEventListener("change", selectGroup);
  byId("auto").addEventListener("change", schedule);
  document.addEventListener("visibilitychange", schedule);
  await refresh();
}

void start();
