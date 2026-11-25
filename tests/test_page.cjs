/* Exercise page event handlers with the documented Dashboard bridge shape. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page(options = {}) {
  const elements = new Map();
  const posts = [];
  const reads = [];
  const timers = new Map();
  let nextTimer = 0;
  function schedule(callback, repeat) {
    const id = ++nextTimer;
    timers.set(id, () => {
      if (!repeat) timers.delete(id);
      return callback();
    });
    return id;
  }
  const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "../_conf_schema.json"), "utf8"));
  schema.daily_calls = { ...schema.daily_calls, minimum: 0, maximum: 100 };
  schema.thinking_notice_text = { ...schema.thinking_notice_text, maxLength: 1000 };
  const view = {
    status: { paused: false, enabled: true, learning_enabled: true },
    persona: { id: "可可子", core: "原核心", versions: [] },
    profile: {}, memories: [], scopes: [], styles: [], style_versions: [],
    config: { values: Object.fromEntries(Object.entries(schema).map(([key, field]) => [key, field.default])), schema, revision: "r1" },
    choices: { personas: [{ id: "可可子", label: "可可子" }], providers: [{ id: "cheap-model", label: "学习模型" }] },
    simulation_models: [{id:"Qwen3.5-2B", label:"2B", kind:"local"}, {id:"Qwen3-8B", label:"8B", kind:"local"}],
    usage: { day: "2026-10-29", summary: {}, daily: [], groups: [], recent: [] },
    schedule: { state: "idle", reason: "等待样本", phase: "regular", groups: [] },
  };
  let refreshError = null;
  function element(tag = "div") {
    const result = {
      value: "", checked: false, tagName: tag.toUpperCase(), dataset: {}, style: {}, disabled: false, children: [], handlers: {},
      classList: { contains: () => false, toggle() {}, add() {} },
      setAttribute() {}, removeAttribute() {}, focus() {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      showModal() { this.open = true; }, close() { this.open = false; },
    };
    Object.defineProperty(result, "id", { set(value) { this._id = value; elements.set(value, this); }, get() { return this._id; } });
    return result;
  }
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    querySelectorAll: () => [],
    createElement: element,
  };
  const sandbox = vm.createContext({
    document, Intl, Date, console,
    window: {
      setTimeout: callback => schedule(callback, false),
      setInterval: callback => schedule(callback, true),
      clearTimeout: id => timers.delete(id),
      clearInterval: id => timers.delete(id),
      addEventListener() {},
      AstrBotPluginPage: {
        ready: async () => ({}),
        apiGet: async endpoint => {
          reads.push(endpoint);
          if (refreshError) throw refreshError;
          return view;
        },
        apiPost: async (endpoint, payload) => {
          posts.push(JSON.parse(JSON.stringify({ endpoint, payload })));
          if (endpoint === "simulation" && options.simulation) return options.simulation(payload);
          if (options.postError) throw options.postError;
          if (options.unconfirmed) return {};
          if (payload.action === "config_save") {
            for (const [name, value] of Object.entries(payload.values)) {
              if (view.config.schema[name]?.secret) {
                view.config.schema[name].configured = Boolean(value);
              } else {
                view.config.values[name] = value;
              }
            }
            view.config.revision = "r2";
            if (payload.values.persona_id) view.persona.id = payload.values.persona_id;
            if (options.refreshAfterSaveError) refreshError = new Error("刷新连接失败");
          }
          return { saved: true };
        },
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../pages/manage/app.js"), "utf8"), sandbox);
  return { elements, posts, reads, timers, view, sandbox };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function simulateModels() {
  let loaded = null;
  let sequence = 0;
  return ({action, model, message}) => {
    if (action === "active" || (action === "select" && !loaded)) {
      return {job: null, loaded_model: loaded, busy: false};
    }
    const unload = action === "unload" || model === "configured" || model.startsWith("bailian:");
    loaded = unload ? null : model;
    const job = {id: String(++sequence), status: "done", model, loaded_model: loaded};
    if (action === "send") {
      return {...job, operation: "send", question: message, answer: "收到", prompt_source: "已保存的人格"};
    }
    return {
      job: {...job, operation: unload ? "unload" : "load", detail: unload ? "模型已释放" : "模型已加载"},
      loaded_model: loaded, busy: false,
    };
  };
}

test("idle panels only refresh on request and a manual refresh can unlock a busy simulator", async () => {
  let otherBusy = true;
  const ui = page({simulation: () => ({job: null, busy: otherBusy, loaded_model: "Qwen3-8B"})});
  await flush();
  const el = id => ui.elements.get(id);
  el("panel-simulation").hidden = true;
  assert.deepEqual(ui.reads, ["state"]);
  assert.equal(ui.timers.size, 0);
  edit(ui, "thinking_notice_text", "正在编辑的设置");
  await el("refresh").handlers.click();
  assert.deepEqual(ui.reads, ["state", "state"]);
  assert.equal(ui.posts.length, 0);
  assert.equal(el("setting-thinking_notice_text").value, "正在编辑的设置");

  el("panel-simulation").hidden = false;
  vm.runInContext('showPanel("simulation")', ui.sandbox);
  await flush();
  assert.equal(el("sim-model").disabled, true);
  assert.equal(ui.timers.size, 0);
  otherBusy = false;
  await el("refresh").handlers.click();
  assert.equal(el("sim-model").disabled, false);
  assert.deepEqual(ui.posts.map(p => p.payload.action), ["active", "active"]);
  assert.equal(ui.timers.size, 0);
});

test("an active reply finishes in the background without rendering while editing settings", async () => {
  const model = "Qwen3.5-2B";
  const job = {id: "test", operation: "send", question: "试聊", model, loaded_model: model};
  const ui = page({simulation: ({action}) => {
    if (action === "active") return {job: null, busy: false, loaded_model: model};
    if (action === "send") return {...job, status: "running"};
    assert.equal(action, "poll");
    return {...job, status: "done", answer: "已完成", prompt_source: "已保存的人格"};
  }});
  await flush();
  const el = id => ui.elements.get(id);
  el("sim-input").value = job.question;
  await el("sim-form").handlers.submit({preventDefault() {}});
  assert.equal(ui.timers.size, 1);
  const visibleMessages = el("sim-messages").children;
  el("panel-simulation").hidden = true;
  const field = el("setting-thinking_notice_text");
  edit(ui, "thinking_notice_text", "写到一半");
  await [...ui.timers.values()][0]();
  assert.equal(ui.timers.size, 0);
  assert.deepEqual(ui.reads, ["state"]);
  assert.equal(el("sim-messages").children, visibleMessages);
  assert.equal(el("setting-thinking_notice_text"), field);
  assert.equal(field.value, "写到一半");
  el("panel-simulation").hidden = false;
  vm.runInContext('showPanel("simulation")', ui.sandbox);
  await flush();
  assert.match(textOf(el("sim-messages")), /已完成/);
  assert.equal(ui.timers.size, 0);
});

test("simulation keeps independent multi-turn chats when switching between 2B and 8B", async () => {
  const simulate = simulateModels();
  const ui = page({simulation: payload => {
    const result = simulate(payload);
    if (payload.action === "send") {
      result.timing = payload.history.length
        ? {load_mode: "resident", first_text_ms: 0, elapsed_ms: 870}
        : {load_mode: "cold", prepare_ms: 1250, load_ms: 2500, first_text_ms: 4010, elapsed_ms: 5030};
    }
    return result;
  }});
  await flush();
  const el = id => ui.elements.get(id);
  const select = async name => {
    el("sim-model").value = name;
    await el("sim-model").handlers.change();
  };
  const send = async message => { el("sim-input").value = message; await el("sim-form").handlers.submit({preventDefault(){}}); };
  assert.equal(el("sim-model").value, "Qwen3.5-2B");
  await send("小模型第一轮");
  await select("Qwen3-8B");
  await send("大模型第一轮");
  await select("Qwen3.5-2B");
  await send("继续刚才的话");
  const log = textOf(el("sim-messages"));
  assert.match(log, /冷启动 · 准备 1\.25 秒 · 加载 2\.50 秒 · 首字 4\.01 秒 · 总耗时 5\.03 秒/);
  assert.match(log, /已驻留 · 首字 0 毫秒 · 总耗时 870 毫秒/);
  assert.doesNotMatch(log, /大模型第一轮/);
  assert.equal(ui.timers.size, 0);
  const chats = ui.posts.filter(p => p.payload.action === "send");
  assert.deepEqual(chats[1].payload.history, []);
  assert.deepEqual(chats[2].payload.history, [{role:"user",content:"小模型第一轮"},{role:"assistant",content:"收到"}]);
  el("sim-clear").handlers.click();
  assert.doesNotMatch(textOf(el("sim-messages")), /继续刚才/);
  await select("Qwen3-8B");
  assert.match(textOf(el("sim-messages")), /大模型第一轮/);
  assert.ok(ui.posts.every(p => p.endpoint === "simulation"));
});

test("plugin mode separates history, reuses its virtual group and resets it on new chat", async () => {
  const ui = page({simulation: p => ({
    ...p, id: "turn", question: p.message, answer: "收到", status: "done",
    session_id: p.enable_tools ? "virtual-group" : null,
    tool_calls: p.enable_tools ? ["lookup"] : [],
  })});
  await flush();
  const el = id => ui.elements.get(id);
  const toggle = on => { el("sim-tools").checked = on; el("sim-tools").handlers.change(); };
  const send = async message => {
    el("sim-input").value = message;
    await el("sim-form").handlers.submit({preventDefault(){}});
    return ui.posts.at(-1).payload;
  };
  assert.equal((await send("普通消息")).enable_tools, false);
  toggle(true);
  const first = await send("查询");
  assert.equal(first.enable_tools, true);
  assert.deepEqual(first.history, []);
  assert.equal(first.session_id, undefined);
  assert.match(textOf(el("sim-messages")), /调用工具 · lookup/);
  toggle(false);
  assert.doesNotMatch(textOf(el("sim-messages")), /lookup|查询/);
  assert.equal((await send("接普通消息")).history[0].content, "普通消息");
  toggle(true);
  const second = await send("接工具消息");
  assert.deepEqual(second.history, [{role:"user",content:"查询"}, {role:"assistant",content:"收到"}]);
  assert.equal(second.session_id, "virtual-group");
  el("sim-clear").handlers.click();
  const fresh = await send("另开一组");
  assert.deepEqual(fresh.history, []);
  assert.equal(fresh.session_id, undefined);
});

test("recovering an in-flight plugin chat restores and locks its checkbox", async () => {
  const job = {id:"j", model:"Qwen3-8B", persona:"可可子", question:"调用", status:"running", enable_tools:true, session_id:"sid"};
  const ui = page({simulation: p => p.action === "active"
    ? {job, busy:true} : {...job, status:"done", answer:"好", tool_calls:["lookup"]}});
  await flush();
  await vm.runInContext("recoverSim(true)", ui.sandbox);
  const el = id => ui.elements.get(id);
  assert.equal(el("sim-tools").checked, true);
  assert.equal(el("sim-tools").disabled, true);
  await [...ui.timers.values()][0]();
  assert.equal(el("sim-tools").disabled, false);
  assert.match(textOf(el("sim-messages")), /lookup/);
});

test("manual load and release update residency without changing the draft or chat", async () => {
  const simulate = simulateModels();
  const ui = page({simulation: payload => {
    const result = simulate(payload);
    if (result.job) result.job.timing = {elapsed_ms: 750};
    return result;
  }});
  await flush();
  const el = id => ui.elements.get(id);
  el("sim-input").value = "还没发送的草稿";
  const engineButton = el("sim-engine");
  assert.equal(engineButton.disabled, false);
  assert.equal(engineButton.textContent, "加载模型");
  await engineButton.handlers.click();
  assert.equal(el("sim-input").value, "还没发送的草稿");
  assert.match(el("sim-resident").textContent, /Qwen3\.5-2B/);
  assert.match(el("sim-status").textContent, /模型已加载 · 总耗时 750 毫秒/);
  assert.equal(engineButton.textContent, "释放模型");
  assert.match(textOf(el("sim-messages")), /第一条消息/);
  await el("sim-form").handlers.submit({preventDefault() {}});
  await engineButton.handlers.click();
  assert.match(el("sim-resident").textContent, /没有已加载/);
  assert.match(el("sim-status").textContent, /模型已释放 · 总耗时 750 毫秒/);
  assert.match(textOf(el("sim-messages")), /还没发送的草稿/);
  assert.equal(engineButton.textContent, "加载模型");
  el("sim-model").value = "configured";
  await el("sim-model").handlers.change();
  assert.equal(engineButton.hidden, true);
});

test("a configured provider shows only its completed round time across manual refresh", async () => {
  const simulate = simulateModels();
  const ui = page({simulation: payload => {
    const result = simulate(payload);
    if (payload.action === "send") result.timing = {load_mode: null, prepare_ms: null, load_ms: null, first_text_ms: null, elapsed_ms: 1234};
    return result;
  }});
  await flush();
  const el = id => ui.elements.get(id);
  ui.view.config.values.local_chat_provider_id = "my-model";
  ui.view.simulation_models.push({id: "configured", label: "自定义模型", kind: "configured"});
  await el("refresh").handlers.click();
  el("sim-model").value = "configured";
  await el("sim-model").handlers.change();
  el("sim-input").value = "试一下";
  await el("sim-form").handlers.submit({preventDefault() {}});
  await el("refresh").handlers.click();
  assert.equal(el("sim-messages").children.at(-1).children.at(-1).textContent, "总耗时 1.23 秒");
  assert.equal(ui.timers.size, 0);
});

test("Bailian selections isolate deployments and explain missing server credentials", async () => {
  const ui = page({simulation: simulateModels()});
  await flush();
  const el = id => ui.elements.get(id);
  const first = {id: "bailian:ws-test:four-b", label: "阿里云百炼 · four-b", kind: "bailian"};
  const second = {...first, id: "bailian:llm-qd5s21mahp3999y0:qwen3-8b-d39bed90dfab", label: "阿里云百炼 · 群聊微调 Qwen3-8B v2"};
  ui.view.simulation_models.push(first, second);
  await el("refresh").handlers.click();
  const select = async model => {
    el("sim-model").value = model.id;
    await el("sim-model").handlers.change();
  };
  const send = async message => {
    el("sim-input").value = message;
    el("sim-input").handlers.input();
    await el("sim-form").handlers.submit({preventDefault() {}});
  };
  await select(first);
  assert.equal(el("sim-engine").hidden, true);
  assert.match(el("sim-model-note").textContent, /阿里云百炼/);
  await send("第一轮");
  await select(second);
  assert.doesNotMatch(textOf(el("sim-messages")), /第一轮/);
  await send("第二个部署");
  await select(first);
  await send("继续");
  const sends = () => ui.posts.filter(p => p.payload.action === "send");
  assert.deepEqual(sends().map(p => p.payload.history.length), [0, 0, 2]);
  assert.match(el("sim-status").textContent, /阿里云百炼 · four-b/);
  first.unavailable_reason = "请设置 DASHSCOPE_API_KEY";
  await el("refresh").handlers.click();
  await send("缺少密钥");
  assert.equal(el("sim-send").disabled, true);
  assert.match(el("sim-model-note").textContent, /DASHSCOPE_API_KEY/);
  assert.equal(sends().length, 3);
  assert.ok(ui.posts.every(p => !["load","unload"].includes(p.payload.action)));
});

test("an old status response cannot undo a manual load and a foreign job locks the controls", async () => {
  const simulate = simulateModels();
  let releaseStatus;
  let active = () => new Promise(resolve => { releaseStatus = resolve; });
  const ui = page({simulation: payload => payload.action === "active" ? active() : simulate(payload)});
  await flush();
  const el = id => ui.elements.get(id);
  const pending = vm.runInContext("recoverSim()", ui.sandbox);
  await el("sim-engine").handlers.click();
  releaseStatus({loaded_model: null, job: null, busy: false});
  await pending;
  assert.match(el("sim-resident").textContent, /Qwen3\.5-2B/);
  active = () => ({loaded_model: "Qwen3-8B", job: null, busy: true});
  await vm.runInContext("recoverSim(true)", ui.sandbox);
  assert.equal(el("sim-model").value, "Qwen3-8B");
  assert.equal(el("sim-engine").disabled, true);
  assert.match(el("sim-resident").textContent, /另一个页面/);
  active = () => ({loaded_model: "Qwen3-8B", job: null, busy: false});
  await vm.runInContext("recoverSim()", ui.sandbox);
  assert.equal(el("sim-engine").disabled, false);
});

test("simulation shows preparation progress, blocks duplicates and restores cancelled input", async () => {
  const base = {id: "pending", model: "Qwen3-8B", question: "试一下"};
  const ui = page({simulation: p => p.action === "cancel"
    ? {...base, status: "cancelled", detail: "已停止", timing: {elapsed_ms: 4100}}
    : {...base, status: "running", detail: "下载模型 · 30%", timing: {elapsed_ms: 3200, first_text_ms: null}}});
  await flush();
  const el = id => ui.elements.get(id);
  el("sim-model").value = base.model; el("sim-input").value = base.question;
  await el("sim-form").handlers.submit({preventDefault(){}});
  assert.equal(el("sim-model").disabled, true);
  assert.match(el("sim-status").textContent, /30%/);
  assert.equal(el("sim-timing").textContent, "已用时 3.20 秒");
  await el("sim-form").handlers.submit({preventDefault(){}});
  assert.equal(ui.posts.length, 1);
  await el("sim-stop").handlers.click();
  assert.equal(el("sim-timing").hidden, true);
  assert.match(el("sim-status").textContent, /已停止 · 总耗时 4\.10 秒/);
  assert.equal(el("sim-input").value, base.question);
  assert.equal(el("sim-model").disabled, false);
  assert.equal(el("sim-clear").disabled, true);
});

test("classified correction review displays the hold and confirms its exact revision", async () => {
  const ui = page(); await flush();
  ui.view.memory_catalog = { total: 1, items: [{ id: 7, user_id: "123456", category: "relationship", status: "pending", hold_reason: "correction", revision: 3, key: "称呼", content: "称呼我小青" }] };
  await vm.runInContext("loadState()", ui.sandbox);
  const list = ui.elements.get("catalog-items");
  assert.match(textOf(list), /纠错提案待核对，原记忆继续有效/);
  assert.doesNotMatch(textOf(list), /知识证据尚未达到/);
  const find = el => el.textContent === "确认纠错" ? el : el.children.map(find).find(Boolean);
  const pending = find(list).handlers.click();
  assert.equal(ui.posts.length, 0);
  ui.elements.get("dialog-form").handlers.submit({ preventDefault() {} }); await pending;
  assert.equal(ui.posts[0].payload.action, "memory_approve");
  assert.equal(ui.posts[0].payload.memory_id, 7);
  assert.equal(ui.posts[0].payload.revision, 3);
});

test("manual dream from settings requires confirmation and preserves the draft persona", async () => {
  const ui = page(); await flush();
  edit(ui, "persona_id", "未保存人格");
  const auto = ui.elements.get("setting-auto_dream"); auto.checked = false; auto.handlers.change();
  ui.elements.get("settings-nav-lifecycle").handlers.click();
  const cancelled = ui.elements.get("settings-dream-now").handlers.click();
  assert.equal(ui.elements.get("action-dialog").open, true);
  assert.match(ui.elements.get("dialog-description").textContent, /人格「可可子」/);
  assert.match(ui.elements.get("dialog-description").textContent, /下次聊天从空的对话历史开始/);
  assert.equal(ui.posts.length, 0);
  ui.elements.get("dialog-cancel").handlers.click(); await cancelled;
  assert.equal(ui.posts.length, 0);
  const confirmed = ui.elements.get("settings-dream-now").handlers.click();
  ui.view.last_dream = 1794355200;
  ui.elements.get("dialog-form").handlers.submit({ preventDefault() {} }); await confirmed;
  assert.equal(ui.posts.length, 1);
  assert.equal(ui.posts[0].payload.action, "memory_dream");
  assert.equal(ui.posts[0].payload.persona_id, "可可子");
  assert.equal(ui.elements.get("setting-persona_id").value, "未保存人格");
  assert.equal(ui.elements.get("setting-auto_dream").checked, false);
  assert.match(ui.elements.get("config-dirty").textContent, /2 项未保存修改/);
  assert.match(ui.elements.get("settings-dream-status").textContent, /最近入梦：2026/);
  assert.equal(ui.elements.get("config-section-lifecycle").hidden, false);
});

test("unconfirmed manual dream does not report success or replace the last dream time", async () => {
  const ui = page({ unconfirmed: true }); await flush();
  const before = ui.elements.get("settings-dream-status").textContent;
  const pending = ui.elements.get("settings-dream-now").handlers.click();
  ui.elements.get("dialog-form").handlers.submit({ preventDefault() {} }); await pending;
  assert.match(ui.elements.get("notice").textContent, /未确认保存/);
  assert.equal(ui.elements.get("settings-dream-status").textContent, before);
});

test("memory setting sliders preserve drafts across sections and save numeric values", async () => {
  const ui = page(); await flush();
  ui.elements.get("settings-nav-lifecycle").handlers.click();
  const slider = ui.elements.get("range-short_cache_capacity");
  slider.value = "45"; slider.handlers.input();
  assert.equal(ui.elements.get("setting-short_cache_capacity").value, "45");
  assert.match(textOf(ui.elements.get("settings-type-cache")), /45 条 \/ 用户/);
  ui.elements.get("settings-nav-budget").handlers.click();
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("config-section-budget").hidden, false);
  assert.equal(ui.elements.get("config-section-lifecycle").hidden, true);
  assert.match(ui.elements.get("settings-preview-state").textContent, /尚未生效/);
  assert.equal(ui.posts.length, 0);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values, { short_cache_capacity: 45 });
  assert.equal(ui.elements.get("config-section-budget").hidden, false);
});

test("lifecycle previews follow numeric input and automatic dream drafts", async () => {
  const ui = page(); await flush();
  edit(ui, "memory_batch_threshold", "25");
  assert.equal(ui.elements.get("range-memory_batch_threshold").value, "25");
  assert.match(textOf(ui.elements.get("settings-flow")), /25 条后整理/);
  edit(ui, "dream_hour", "6");
  assert.match(textOf(ui.elements.get("settings-type-daily")), /06:00 入梦清理/);
  const automatic = ui.elements.get("setting-auto_dream"); automatic.checked = false; automatic.handlers.change();
  assert.match(textOf(ui.elements.get("settings-type-daily")), /下次手动入梦/);
  edit(ui, "short_cache_capacity", "");
  assert.match(textOf(ui.elements.get("settings-type-cache")), /待填写有效值/);
  assert.doesNotMatch(textOf(ui.elements.get("settings-type-cache")), /0 条/);
  assert.equal(ui.posts.length, 0);
});

test("budget preview distinguishes missing usage, exhausted quotas and zero limits", async () => {
  const ui = page(); await flush();
  assert.match(textOf(ui.elements.get("settings-budget-daily")), /暂未取得用量数据/);
  ui.view.memory_usage = { daily: { mtokens: 0.03 }, monthly: { mtokens: 0 } };
  await vm.runInContext("loadState()", ui.sandbox);
  edit(ui, "memory_daily_mtokens", "0.02");
  assert.match(textOf(ui.elements.get("settings-budget-daily")), /已达到此限额/);
  assert.match(textOf(ui.elements.get("settings-budget-monthly")), /0.000000.*剩余 1.800000/);
  edit(ui, "memory_daily_mtokens", "0");
  assert.match(textOf(ui.elements.get("settings-budget-daily")), /模型整理停用/);
  edit(ui, "memory_monthly_mtokens", "");
  assert.match(textOf(ui.elements.get("settings-budget-monthly")), /请填写有效限额/);
  assert.doesNotMatch(textOf(ui.elements.get("settings-budget-preview")), /NaN|Infinity/);
});

test("invalid values in another settings section reveal the field without posting", async () => {
  const ui = page(); await flush();
  edit(ui, "daily_calls", "oops");
  ui.elements.get("settings-nav-lifecycle").handlers.click();
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.equal(ui.elements.get("config-section-budget").hidden, false);
  assert.match(ui.elements.get("notice").textContent, /请填写整数/);
  assert.equal(ui.elements.get("setting-daily_calls").value, "oops");
  assert.equal(ui.posts.length, 0);
});

test("memory classifier approval sends revision and migration definition", async () => {
  const ui=page(); await flush();
  const cls={id:"travel",name:"出行",category:"relationship",description:"出行认识",keywords:["旅行"],ttl_seconds:1800,status:"pending",revision:3};
  ui.view.classifiers=[cls];
  await vm.runInContext("loadState()",ui.sandbox);
  vm.runInContext(`editClassifier(${JSON.stringify(cls)})`,ui.sandbox);
  ui.elements.get("classifier-category").value="timed";
  ui.elements.get("classifier-ttl").value="300";
  ui.elements.get("classifier-confirm").checked=true;
  await ui.elements.get("classifier-form").handlers.submit({preventDefault(){}});
  assert.equal(ui.posts[0].payload.action,"classifier_save");
  assert.equal(ui.posts[0].payload.revision,3);
  assert.equal(ui.posts[0].payload.definition.category,"timed");
  assert.equal(ui.posts[0].payload.definition.ttl_seconds,300);
  assert.equal(ui.posts[0].payload.confirm,true);
});

test("memory detail editing retains evidence and reports conflicts", async () => {
  const ui=page({postError:new Error("记忆已改变，请刷新")}); await flush();
  const record={id:5,user_id:"123456",revision:2,class_id:"relationship",key:"称呼",content:"叫我小青",provenance:"self_report",accuracy:"supported",confidence:0.9,mood:"开心",salience:0.3,ttl_seconds:1800,tags:["称呼"],details:{address:"小青"}};
  ui.view.classifiers=[{id:"relationship",name:"关系"}];
  vm.runInContext(`editMemoryDetails(${JSON.stringify(record)})`,ui.sandbox);
  ui.elements.get("detail-mood").value="平静";
  await ui.elements.get("memory-detail-form").handlers.submit({preventDefault(){}});
  assert.equal(ui.posts[0].payload.action,"memory_detail_save");
  assert.equal(ui.posts[0].payload.revision,2);
  assert.equal(ui.posts[0].payload.changes.mood,"平静");
  assert.equal(ui.posts[0].payload.changes.details.address,"小青");
  assert.equal(ui.elements.get("memory-detail-dialog").open,true);
  assert.match(ui.elements.get("memory-detail-error").textContent,/记忆已改变/);
});

test("Mtokens inputs stay numeric and public memory is opt in", async () => {
  const ui=page(); await flush();
  ui.view.groups=[knownGroup()]; await vm.runInContext("loadState()",ui.sandbox);
  assert.equal(ui.elements.get("group-public-qq-main-234567").checked,false);
  const pub=ui.elements.get("group-public-qq-main-234567"); pub.checked=true; pub.handlers.change();
  edit(ui,"memory_daily_mtokens","0.125");
  edit(ui,"memory_monthly_mtokens","2.5");
  await ui.elements.get("settings-form").handlers.submit({preventDefault(){}});
  assert.equal(ui.posts[0].payload.values.memory_daily_mtokens,0.125);
  assert.equal(ui.posts[0].payload.values.memory_monthly_mtokens,2.5);
  assert.deepEqual(ui.posts[0].payload.values.public_memory_groups,["qq-main:234567"]);
});

test("turning off default memory saves the explicit group exclusion", async () => {
  const ui=page(); await flush();
  ui.view.groups=[knownGroup()]; await vm.runInContext("loadState()",ui.sandbox);
  const control=ui.elements.get("group-memory-qq-main-234567"); control.checked=false; control.handlers.change();
  await ui.elements.get("settings-form").handlers.submit({preventDefault(){}});
  assert.deepEqual(ui.posts[0].payload.values.memory_disabled_groups,["qq-main:234567"]);
});
const textOf = (element) => [element.textContent || "", ...element.children.map(textOf)].join(" ");
const knownGroup = (platform = "qq-main", groupId = "234567", name = "鼓友练习群") => ({
  platform_id: platform, group_id: groupId, group_name: name, bot_id: "345678",
  first_seen: 1793246400, last_seen: 1793246400, member_count: 42, name_status: "available",
});
function edit(ui, key, value) {
  const field = ui.elements.get(`setting-${key}`);
  if (field.type === "checkbox") field.checked = value;
  else field.value = value;
  (field.handlers.input || field.handlers.change)();
}

test("sent conversation remains visible before any expression is learned", async () => {
  const ui = page(); await flush();
  ui.view.dialogues = [{user_id:"123456", user_text:"明天见", reply_text:"明天见呀", received_at:1793246400, feedback:""}];
  await vm.runInContext('selectedScope = "qq:bot:group"; loadState()', ui.sandbox);
  const text = textOf(ui.elements.get("styles"));
  assert.match(text, /还没有学习到表达/);
  assert.match(text, /明天见呀/);
  assert.match(text, /未知/);
});

for (const mode of ["saved", "conflict", "unconfirmed"]) {
  test(`expression editing preserves conditions and revision: ${mode}`, async () => {
    const ui = page(mode === "conflict" ? {postError:new Error("表达已更新")} : {unconfirmed:mode === "unconfirmed"});
    await flush();
    vm.runInContext('editExpression({id:9, revision:4, form_type:"response", situation:"对方道别", expression:"简短道别后停住", avoid:["还有问题"], intent:"closing", keywords:[]})', ui.sandbox);
    ui.elements.get("expression-keywords").value = "明天见，晚安";
    await ui.elements.get("expression-form").handlers.submit({preventDefault(){}, submitter:{disabled:false}});
    assert.equal(ui.posts[0].payload.action, "style_edit");
    assert.equal(ui.posts[0].payload.revision, 4);
    assert.deepEqual(ui.posts[0].payload.form.keywords, ["明天见","晚安"]);
    assert.equal(ui.posts[0].payload.form.intent, "closing");
    if (mode === "saved") {
      assert.equal(ui.elements.get("expression-dialog").open, false);
      assert.match(ui.elements.get("notice").textContent, /待审核/);
    } else {
      assert.equal(ui.elements.get("expression-dialog").open, true);
      assert.equal(ui.elements.get("expression-pattern").value, "简短道别后停住");
      assert.equal(ui.elements.get("expression-error").hidden, false);
    }
  });
}

test("global toggle and tag editor persist without enabling collection", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup()];
  await vm.runInContext("loadState()", ui.sandbox);
  const tag = ui.elements.get("group-tags-qq-main-234567");
  tag.value = "太鼓达人、轻松"; tag.handlers.input();
  // Input must stay mounted: Chinese IME and caret must not be lost on each key.
  assert.equal(ui.elements.get(tag.id), tag);
  const control = ui.elements.get("group-global-qq-main-234567");
  control.checked = true; control.handlers.change();
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get(tag.id).value, "太鼓达人、轻松");
  assert.equal(ui.elements.get("group-observation-qq-main-234567").checked, false);
  assert.equal(ui.elements.get("group-memory-qq-main-234567").checked, true);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values, {global_style_groups:["qq-main:234567"], group_tags:["qq-main:234567=太鼓达人、轻松"]});
});

test("tag removal and global disabling preserve other connections and unknown groups", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup(), knownGroup("qq-second")];
  ui.view.config.values.group_tags = ["234567=轻松", "qq-other:999999=读书"];
  ui.view.config.values.global_style_groups = ["234567", "qq-other:999999"];
  await vm.runInContext("loadState()", ui.sandbox);
  const tag = ui.elements.get("group-tags-qq-main-234567"); tag.value = ""; tag.handlers.input();
  const control = ui.elements.get("group-global-qq-main-234567"); control.checked = false; control.handlers.change();
  assert.equal(ui.elements.get("group-tags-qq-second-234567").value, "轻松");
  assert.equal(ui.elements.get("group-tags-qq-main-234567").value, "");
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values.group_tags, ["qq-other:999999=读书", "qq-second:234567=轻松"]);
  assert.deepEqual(ui.posts[0].payload.values.global_style_groups, ["qq-other:999999", "qq-second:234567"]);
});

test("tag search and stale revision keep and protect the draft", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup()];
  await vm.runInContext("loadState()", ui.sandbox);
  const tag = ui.elements.get("group-tags-qq-main-234567"); tag.value = "技术"; tag.handlers.input();
  ui.elements.get("group-search").value = "技术"; ui.elements.get("group-search").handlers.input();
  assert.match(textOf(ui.elements.get("group-list")), /鼓友/);
  ui.view.config.revision = "other-editor";
  await vm.runInContext("loadState()", ui.sandbox);
  const fresh = ui.elements.get(tag.id);
  assert.equal(fresh.value, "技术");
  assert.equal(fresh.disabled, true);
  fresh.value = "discard"; fresh.handlers.input();
  assert.equal(ui.elements.get("setting-group_tags").value, "qq-main:234567=技术");
  assert.equal(ui.elements.get("group-global-qq-main-234567").disabled, true);
});

test("failed global setting save preserves tags and switch drafts", async () => {
  const ui = page({postError: new Error("写入失败")}); await flush();
  ui.view.groups = [knownGroup()];
  await vm.runInContext("loadState()", ui.sandbox);
  const tag = ui.elements.get("group-tags-qq-main-234567"); tag.value = "轻松"; tag.handlers.input();
  const control = ui.elements.get("group-global-qq-main-234567"); control.checked = true; control.handlers.change();
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.match(ui.elements.get("notice").textContent, /写入失败/);
  assert.equal(ui.elements.get(tag.id).value, "轻松");
  assert.equal(ui.elements.get(control.id).checked, true);
});

for (const [input, expected] of [["昨天", null], ["", null], ["2026-10-20", "2026-10-20"]]) {
  test(`editing a historical memory sends only changed time: ${JSON.stringify(input)}`, async () => {
    const ui = page();
    await flush();
    const pending = vm.runInContext(
      'correctMemory({id: 8, content: "我昨天练了幽玄", state: "in_progress", time_text: "昨天"})',
      ui.sandbox,
    );
    assert.equal(ui.elements.get("dialog-time").value, "昨天");
    ui.elements.get("dialog-content").value = "修正后的历史练习";
    ui.elements.get("dialog-time").value = input;
    ui.elements.get("dialog-form").handlers.submit({ preventDefault() {} });
    await pending;
    assert.equal(ui.posts.length, 1);
    assert.equal(ui.posts[0].endpoint, "action");
    assert.equal(ui.posts[0].payload.action, "memory_correct");
    assert.equal(ui.posts[0].payload.memory_id, "8");
    assert.equal(ui.posts[0].payload.time_expression, expected);
    assert.equal(ui.posts[0].payload.state, "in_progress");
  });
}

test("configuration-disabled learning never displays as enabled", async () => {
  const ui = page();
  await flush();
  assert.equal(ui.elements.get("learning-status").textContent, "学习已开启");
  ui.view.status.learning_enabled = false;
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("learning-status").textContent, "配置已关闭学习");
  ui.view.status.learning_enabled = true;
  ui.view.status.enabled = false;
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("learning-status").textContent, "配置已关闭学习");
});

test("all server configuration fields render with provider/persona suggestions", async () => {
  const ui = page();
  await flush();
  const fields = Object.keys(ui.view.config.schema);
  assert.ok(fields.length >= 31);
  for (const field of fields) assert.ok(ui.elements.has(`setting-${field}`), `${field} was omitted`);
  assert.equal(ui.elements.get("settings-count").textContent, `${fields.length} 项设置`);
  assert.equal(ui.elements.get("setting-evolution_guidance").tagName, "TEXTAREA");
  assert.ok(ui.elements.has("choices-persona_id"));
  assert.ok(ui.elements.has("choices-learning_provider_id"));
  assert.ok(ui.elements.has("choices-local_chat_provider_id"));
  assert.equal(ui.elements.get("config-save").disabled, true);
});

test("Bailian key is a write-only password field and other edits preserve it", async () => {
  const ui = page();
  await flush();
  const field = () => ui.elements.get("setting-bailian_api_key");
  assert.equal(field().type, "password");
  edit(ui, "bailian_api_key", "plugin-test-key");
  await ui.elements.get("settings-form").handlers.submit({preventDefault() {}});
  assert.deepEqual(ui.posts[0].payload.values, {bailian_api_key: "plugin-test-key"});
  assert.equal(field().value, "");
  assert.match(field().placeholder, /已配置/);
  assert.doesNotMatch(JSON.stringify(ui.view), /plugin-test-key/);
  edit(ui, "daily_calls", "8");
  await ui.elements.get("settings-form").handlers.submit({preventDefault() {}});
  assert.deepEqual(ui.posts[1].payload.values, {daily_calls: 8});
});

test("configuration saves only edited values against the original revision", async () => {
  const ui = page();
  await flush();
  edit(ui, "daily_calls", "9");
  edit(ui, "observation_groups", "123456\n\n 778899 \n123456");
  edit(ui, "price_aware_learning", false);
  edit(ui, "evolution_guidance", "少复读，优先记住练习进展");
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.equal(ui.posts.length, 1);
  assert.equal(ui.posts[0].payload.action, "config_save");
  assert.equal(ui.posts[0].payload.revision, "r1");
  assert.deepEqual(ui.posts[0].payload.values, { daily_calls: 9, observation_groups: ["123456", "778899"], price_aware_learning: false, evolution_guidance: "少复读，优先记住练习进展" });
  assert.equal(ui.elements.get("config-dirty").textContent, "设置已同步");
  assert.equal(ui.elements.get("config-save").disabled, true);
});

test("group notice content supports multiline drafts, saving and silent mode", async () => {
  const ui = page(); await flush();
  const field = ui.elements.get("setting-thinking_notice_text");
  assert.equal(field.tagName, "TEXTAREA");
  assert.equal(field.maxLength, 1000);
  assert.equal(vm.runInContext('settingsPanelFor("thinking_notice_text")', ui.sandbox), "rhythm");
  const text = "让我想一想～\n整理一下刚才的聊天。";
  edit(ui, "thinking_notice_text", text);
  ui.elements.get("settings-nav-budget").handlers.click();
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("setting-thinking_notice_text").value, text);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values, { thinking_notice_text: text });
  edit(ui, "thinking_notice_text", " \n ");
  assert.match(textOf(ui.elements.get("settings-flow")), /仅控制台显示思考状态/);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[1].payload.values, { thinking_notice_text: "" });
  assert.equal(ui.elements.get("setting-thinking_notice_text").value, "");
});

test("overlong group notice stays in the draft and points to rhythm settings", async () => {
  const ui = page(); await flush();
  edit(ui, "thinking_notice_text", "长".repeat(1001));
  ui.elements.get("settings-nav-budget").handlers.click();
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.equal(ui.posts.length, 0);
  assert.equal(ui.elements.get("setting-thinking_notice_text").value.length, 1001);
  assert.match(ui.elements.get("notice").textContent, /群内思考提示内容最多 1000 字/);
  assert.equal(vm.runInContext("selectedSettingsPanel", ui.sandbox), "rhythm");
});

test("invalid integer does not send a save or drop the draft", async () => {
  const ui = page();
  await flush();
  edit(ui, "daily_calls", "101");
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.equal(ui.posts.length, 0);
  assert.match(ui.elements.get("notice").textContent, /超出允许范围/);
  assert.equal(ui.elements.get("setting-daily_calls").value, "101");
});

test("refresh preserves unsaved settings and blocks a stale revision", async () => {
  const ui = page();
  await flush();
  edit(ui, "daily_calls", "9");
  ui.view.config.values.daily_calls = 7;
  ui.view.config.revision = "external-change";
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("setting-daily_calls").value, "9");
  assert.equal(ui.elements.get("config-conflict").hidden, false);
  assert.equal(ui.elements.get("config-save").disabled, true);
  await vm.runInContext("saveSettings()", ui.sandbox);
  assert.equal(ui.posts.length, 0);
});

test("backend conflict preserves draft and never reports save success", async () => {
  const ui = page({ postError: new Error("配置已被其他页面修改，请重新载入并核对草稿") });
  await flush();
  edit(ui, "daily_calls", "9");
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.equal(ui.elements.get("setting-daily_calls").value, "9");
  assert.equal(ui.elements.get("config-conflict").hidden, false);
  assert.match(ui.elements.get("notice").textContent, /配置已被其他页面修改/);
  assert.equal(ui.elements.get("notice").className, "notice error");
});

test("unconfirmed backend response is not treated as a successful save", async () => {
  const ui = page({ unconfirmed: true });
  await flush();
  edit(ui, "daily_calls", "9");
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.match(ui.elements.get("notice").textContent, /未确认/);
  assert.equal(ui.elements.get("setting-daily_calls").value, "9");
  assert.equal(ui.elements.get("config-dirty").textContent, "1 项未保存修改");
});

test("save followed by refresh failure blocks duplicate submission", async () => {
  const ui = page({ refreshAfterSaveError: true });
  await flush();
  edit(ui, "daily_calls", "9");
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.match(ui.elements.get("notice").textContent, /配置已保存，但刷新失败/);
  assert.equal(ui.elements.get("config-save").disabled, true);
  await vm.runInContext("saveSettings()", ui.sandbox);
  assert.equal(ui.posts.length, 1);
});

test("explicit reload confirms draft loss and accepts the latest revision", async () => {
  const ui = page();
  await flush();
  edit(ui, "daily_calls", "9");
  ui.view.config.values.daily_calls = 7;
  ui.view.config.revision = "external-change";
  const pending = ui.elements.get("config-reload").handlers.click();
  assert.equal(ui.elements.get("action-dialog").open, true);
  ui.elements.get("dialog-form").handlers.submit({ preventDefault() {} });
  await pending;
  assert.equal(ui.elements.get("setting-daily_calls").value, "7");
  assert.equal(ui.elements.get("config-conflict").hidden, true);
  assert.equal(ui.elements.get("config-dirty").textContent, "设置已同步");
});

test("usage separates measured, estimated and reserved tokens and renders zero honestly", async () => {
  const ui = page();
  await flush();
  ui.view.usage.summary = { calls: 3, input_tokens: 100, output_tokens: 20, reported_tokens: 120, estimated_tokens: 400, reserved_tokens: 80, charged_tokens: 600, failed_calls: 0, unknown_calls: 1 };
  ui.view.usage.daily = [{ day: "2026-10-29", charged_tokens: 600 }];
  ui.view.usage.groups = [{ group_id: "123456", calls: 3, reported_tokens: 120, charged_tokens: 600 }];
  ui.view.usage.recent = [{ id: 1, purpose: "interaction", status: "success", total_tokens: 0, input_tokens: 0, output_tokens: 0, accepted: 0, metadata: { sample_count: 3, accepted_memories: 0, accepted_styles: 0, phase: "cheap" } }];
  ui.view.schedule = { state: "cooldown", phase: "cheap", reason: "等待学习间隔", groups: [{ group_id: "123456", purpose: "interaction", state: "cooldown", reason: "等待间隔", phase: "cheap", sample_count: 3, minimum: 1 }] };
  await vm.runInContext("loadState()", ui.sandbox);
  const metrics = textOf(ui.elements.get("usage-metrics"));
  assert.match(metrics, /已报告 Token.*120/);
  assert.match(metrics, /估算占用.*400/);
  assert.match(metrics, /在途预留.*80/);
  assert.match(metrics, /失败 0 次/);
  const recent = textOf(ui.elements.get("learning-recent"));
  assert.match(recent, /已完成/);
  assert.doesNotMatch(recent, /已采纳/);
  assert.match(recent, /已报告 0/);
  assert.match(recent, /采纳记忆 0 条/);
  assert.match(textOf(ui.elements.get("schedule-details")), /当前价段：低价/);
  assert.match(textOf(ui.elements.get("schedule-groups")), /样本 3 \/ 门槛 1/);
});

test("overview separates captured dialogue, pending extraction and stored memory without requiring a model call", async () => {
  const ui = page(); await flush();
  Object.assign(ui.view.status, {
    runtime_version: "2.2.1", sources: 24, pending_interactions: 20, memories: 0, pending_memories: 2,
    capture: { state: "captured", reason: "已采集群聊对话，等待整理", group_id: "234567", checked_at: 1794355200, last_seen_at: 1794355190, last_request_at: 1794355195 },
  });
  ui.view.schedule = { state: "budget", reason: "预计 20009 tokens，单次上限 20000 tokens", groups: [] };
  await vm.runInContext("loadState()", ui.sandbox);
  const detail = textOf(ui.elements.get("schedule-details"));
  assert.match(detail, /运行版本：v2\.2\.1 \/ 页面 v2\.10\.0/);
  assert.match(detail, /已采集原文：24/);
  assert.match(detail, /待整理对话：20/);
  assert.match(detail, /生效记忆：0/);
  assert.match(detail, /待确认记忆：2/);
  assert.match(detail, /最近采集检查：已采集群聊对话，等待整理/);
  assert.match(detail, /采集检查群：234567/);
  assert.match(detail, /采集检查时间：2026/);
  assert.match(detail, /最近收到群消息：2026/);
  assert.match(detail, /最近进入模型请求：2026/);
  const recent = textOf(ui.elements.get("learning-recent"));
  assert.match(recent, /没有调用记录不等于没有采集/);
  assert.match(recent, /已采集 24 条原文，有 20 条对话待整理/);
  assert.match(recent, /预计 20009 tokens，单次上限 20000 tokens/);
  assert.doesNotMatch(recent, /尚未采集到原文/);
});

test("empty learning calls explain zero capture, paused and disabled learning with the latest gate reason", async () => {
  const ui = page(); await flush();
  Object.assign(ui.view.status, { sources: 0, pending_interactions: 0, memories: 0, pending_memories: 0, capture: { state: "blocked", reason: "门禁插件未在当前会话启用" } });
  await vm.runInContext("loadState()", ui.sandbox);
  assert.match(textOf(ui.elements.get("learning-recent")), /尚未采集到原文/);
  assert.match(textOf(ui.elements.get("learning-recent")), /门禁插件未在当前会话启用/);
  for (const [values, expected] of [
    [{ paused: true }, /学习已暂停/],
    [{ paused: false, learning_enabled: false }, /学习已关闭/],
    [{ learning_enabled: true, enabled: false }, /插件已关闭/],
  ]) {
    Object.assign(ui.view.status, values);
    await vm.runInContext("loadState()", ui.sandbox);
    const recent = textOf(ui.elements.get("learning-recent"));
    assert.match(recent, expected);
    assert.doesNotMatch(recent, /尚未采集到原文；请让/);
  }
});

test("legacy overview diagnostics remain unknown and translate active scheduler states", async () => {
  const ui = page(); await flush();
  const detail = textOf(ui.elements.get("schedule-details"));
  assert.match(detail, /运行版本：未知 \/ 页面 v2\.10\.0/);
  assert.match(detail, /已采集原文：—/);
  assert.match(detail, /待整理对话：—/);
  assert.match(detail, /待确认记忆：—/);
  assert.match(detail, /暂无采集诊断/);
  assert.match(textOf(ui.elements.get("learning-recent")), /采集数量暂未取得/);
  assert.doesNotMatch(textOf(ui.elements.get("learning-recent")), /尚未采集到原文|已采集 0/);
  for (const [state, label] of [["collecting", "积累对话"], ["thinking", "思考整理中"], ["missing_provider", "缺少学习模型"]]) {
    ui.view.schedule = { state, groups: [{ state, group_id: "234567", purpose: "interaction" }] };
    await vm.runInContext("loadState()", ui.sandbox);
    assert.equal(ui.elements.get("schedule-status").textContent, label);
    assert.match(textOf(ui.elements.get("schedule-groups")), new RegExp(label));
  }
});

test("configuration rendering failures leave current memory diagnostics visible and report the error", async () => {
  const ui = page(); await flush();
  Object.assign(ui.view.status, { sources: 50, pending_interactions: 20, memories: 0, pending_memories: 1 });
  ui.view.schedule = { state: "collecting", reason: "已采集对话，等待下一次整理", groups: [] };
  ui.view.memory_catalog = { total: 1, items: [{ id: 8, user_id: "__knowledge__", key: "太鼓手法", content: "双手交替需要保持节奏", category: "knowledge", status: "pending", hold_reason: "knowledge", revision: 1 }] };
  vm.runInContext('renderConfig = () => { throw new Error("配置面板渲染失败（注入测试）"); };', ui.sandbox);
  await ui.elements.get("refresh").handlers.click();
  assert.equal(ui.elements.get("schedule-status").textContent, "积累对话");
  assert.match(textOf(ui.elements.get("schedule-details")), /已采集原文：50.*待整理对话：20.*待确认记忆：1/);
  assert.match(textOf(ui.elements.get("catalog-items")), /太鼓手法.*双手交替需要保持节奏/);
  assert.match(ui.elements.get("catalog-count").textContent, /共 1 条/);
  const notice = ui.elements.get("notice");
  assert.equal(notice.textContent, "配置面板渲染失败（注入测试）");
  assert.equal(notice.className, "notice error");
  assert.equal(notice.hidden, false);
  assert.equal(ui.posts.length, 0);
});

test("direction example only changes the local draft", async () => {
  const ui = page();
  await flush();
  const fields = ui.elements.get("settings-sections");
  const search = (el) => el.textContent === "载入参考方向" ? el : el.children.map(search).find(Boolean);
  await search(fields).handlers.click();
  assert.match(ui.elements.get("setting-evolution_guidance").value, /少复读/);
  assert.equal(ui.posts.length, 0);
  assert.equal(ui.elements.get("config-dirty").textContent, "1 项未保存修改");
});

test("discovered groups default to local memory with public memory disabled", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup()];
  await vm.runInContext("loadState()", ui.sandbox);
  const text = textOf(ui.elements.get("group-list"));
  assert.match(text, /鼓友练习群/);
  assert.match(text, /234567/);
  assert.match(text, /qq-main/);
  assert.match(text, /42 人/);
  assert.equal(ui.elements.get("group-observation-qq-main-234567").checked, false);
  assert.equal(ui.elements.get("group-memory-qq-main-234567").checked, true);
  assert.equal(ui.elements.get("group-global-qq-main-234567").checked, false);
  assert.equal(ui.posts.length, 0);
});

test("group switches save independent native settings without typing IDs", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup(), knownGroup("qq-main", "456789", "另一个群")];
  await vm.runInContext("loadState()", ui.sandbox);
  for (const key of ["group-observation-qq-main-234567", "group-memory-qq-main-456789", "group-local-qq-main-234567"]) {
    const control = ui.elements.get(key); control.checked = true; control.handlers.change();
  }
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("group-memory-qq-main-456789").checked, true);
  assert.equal(ui.elements.get("group-local-qq-main-234567").checked, true);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values, {observation_groups:["qq-main:234567"], memory_groups:["qq-main:456789"], local_chat_groups:["qq-main:234567"]});
});

test("Bailian trials select, switch and clear only the chosen group connection", async () => {
  const ui = page(); await flush();
  const prefix = "bailian:ws-test:";
  const eight = prefix + "qwen3-8b-d39bed90dfab";
  const four = prefix + "qwen3-4b-instruct-2507-438f534673d5-v1";
  ui.view.simulation_models.push(
    { id: eight, kind: "bailian", group_trial_label: "群聊微调 Qwen3-8B v2" },
    { id: four, kind: "bailian", group_trial_label: "六爻 Qwen3-4B" },
    { id: prefix + "custom", kind: "bailian", label: "其他试聊部署" },
  );
  ui.view.groups = [knownGroup(), knownGroup("qq-second")];
  const other = "qq-second:234567=" + four;
  const hidden = "offline:456789=" + eight;
  ui.view.config.values.bailian_group_models = [hidden, other];
  ui.view.config.values.local_chat_groups = ["qq-main:234567"];
  await vm.runInContext("loadState()", ui.sandbox);
  const picker = () => ui.elements.get("group-trial-qq-main-234567");
  assert.equal(picker().value, "");
  assert.deepEqual(picker().children.map((option) => option.value), ["", eight, four]);
  assert.equal(ui.posts.length, 0);
  for (const model of [eight, four, ""]) {
    picker().value = model; picker().handlers.change();
    assert.equal(ui.elements.get("group-trial-qq-second-234567").value, four);
    assert.equal(ui.elements.get("group-local-qq-main-234567").checked, true);
    await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
    const expected = [hidden, other];
    if (model) expected.push("qq-main:234567=" + model);
    assert.deepEqual(ui.posts.at(-1).payload.values, {bailian_group_models: expected.sort()});
    assert.equal(picker().value, model);
  }
});

test("disabling one connection preserves another connection and unrelated legacy IDs", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup(), knownGroup("qq-second")];
  ui.view.config.values.observation_groups = ["234567", "777888"];
  await vm.runInContext("loadState()", ui.sandbox);
  const control = ui.elements.get("group-observation-qq-main-234567");
  control.checked = false; control.handlers.change();
  assert.equal(ui.elements.get("group-observation-qq-second-234567").checked, true);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values.observation_groups, ["777888", "qq-second:234567"]);
});

test("memory switch clears only the matching legacy exclusion", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup(), knownGroup("qq-second")];
  ui.view.config.values.memory_disabled_groups = ["234567", "777888"];
  await vm.runInContext("loadState()", ui.sandbox);
  const control = ui.elements.get("group-memory-qq-main-234567");
  control.checked = true; control.handlers.change();
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values.memory_groups, ["qq-main:234567"]);
  assert.deepEqual(ui.posts[0].payload.values.memory_disabled_groups, ["777888", "qq-second:234567"]);
});

test("group search and selection filter preserve all hidden selections", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup(), knownGroup("qq-main", "456789", "读书群")];
  ui.view.config.values.memory_groups = ["qq-main:456789"];
  ui.view.config.values.memory_default_enabled = false;
  await vm.runInContext("loadState()", ui.sandbox);
  ui.elements.get("group-search").value = "鼓友";
  ui.elements.get("group-search").handlers.input();
  assert.match(textOf(ui.elements.get("group-list")), /鼓友/);
  assert.doesNotMatch(textOf(ui.elements.get("group-list")), /读书/);
  ui.elements.get("group-search").value = "";
  ui.elements.get("group-filter").value = "selected";
  ui.elements.get("group-filter").handlers.change();
  assert.match(textOf(ui.elements.get("group-list")), /读书/);
  assert.doesNotMatch(textOf(ui.elements.get("group-list")), /鼓友/);
  assert.equal(ui.elements.get("setting-memory_groups").value, "qq-main:456789");
  assert.equal(ui.posts.length, 0);
});

test("group metadata refresh keeps draft and revision conflict blocks new toggles", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup()];
  await vm.runInContext("loadState()", ui.sandbox);
  let control = ui.elements.get("group-observation-qq-main-234567");
  control.checked = true; control.handlers.change();
  ui.view.groups[0].group_name = "<img src=x onerror=alert(1)>";
  ui.view.config.revision = "other-editor";
  await vm.runInContext("loadState()", ui.sandbox);
  assert.match(textOf(ui.elements.get("group-list")), /<img src=x/);
  control = ui.elements.get("group-memory-qq-main-234567");
  assert.equal(control.disabled, true);
  control.checked = true; control.handlers.change();
  assert.equal(ui.elements.get("setting-memory_groups").value, "");
  assert.equal(ui.elements.get("setting-observation_groups").value, "qq-main:234567");
  assert.equal(ui.posts.length, 0);
});
