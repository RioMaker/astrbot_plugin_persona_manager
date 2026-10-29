/* Exercise page event handlers with the documented Dashboard bridge shape. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page(options = {}) {
  const elements = new Map();
  const posts = [];
  const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "../_conf_schema.json"), "utf8"));
  schema.daily_calls = { ...schema.daily_calls, minimum: 0, maximum: 100 };
  const view = {
    status: { paused: false, enabled: true, learning_enabled: true },
    persona: { id: "可可子", core: "原核心", versions: [] },
    profile: {}, memories: [], scopes: [], styles: [], style_versions: [],
    config: { values: Object.fromEntries(Object.entries(schema).map(([key, field]) => [key, field.default])), schema, revision: "r1" },
    choices: { personas: [{ id: "可可子", label: "可可子" }], providers: [{ id: "cheap-model", label: "学习模型" }] },
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
      addEventListener() {},
      AstrBotPluginPage: {
        ready: async () => ({}),
        apiGet: async () => { if (refreshError) throw refreshError; return view; },
        apiPost: async (endpoint, payload) => {
          posts.push(JSON.parse(JSON.stringify({ endpoint, payload })));
          if (options.postError) throw options.postError;
          if (options.unconfirmed) return {};
          if (payload.action === "config_save") {
            Object.assign(view.config.values, payload.values);
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
  return { elements, posts, view, sandbox };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
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
  assert.equal(ui.elements.get("group-memory-qq-main-234567").checked, false);
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
  assert.equal(ui.elements.get("config-save").disabled, true);
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

test("discovered groups show names and start with three unselected switches", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup()];
  await vm.runInContext("loadState()", ui.sandbox);
  const text = textOf(ui.elements.get("group-list"));
  assert.match(text, /鼓友练习群/);
  assert.match(text, /234567/);
  assert.match(text, /qq-main/);
  assert.match(text, /42 人/);
  assert.equal(ui.elements.get("group-observation-qq-main-234567").checked, false);
  assert.equal(ui.elements.get("group-memory-qq-main-234567").checked, false);
  assert.equal(ui.elements.get("group-global-qq-main-234567").checked, false);
  assert.equal(ui.posts.length, 0);
});

test("group switches save independent native settings without typing IDs", async () => {
  const ui = page(); await flush();
  ui.view.groups = [knownGroup(), knownGroup("qq-main", "456789", "另一个群")];
  await vm.runInContext("loadState()", ui.sandbox);
  for (const key of ["group-observation-qq-main-234567", "group-memory-qq-main-456789"]) {
    const control = ui.elements.get(key); control.checked = true; control.handlers.change();
  }
  await vm.runInContext("loadState()", ui.sandbox);
  assert.equal(ui.elements.get("group-memory-qq-main-456789").checked, true);
  await ui.elements.get("settings-form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(ui.posts[0].payload.values, {observation_groups:["qq-main:234567"], memory_groups:["qq-main:456789"]});
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
