/* Exercise page event handlers with the documented Dashboard bridge shape. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page() {
  const elements = new Map();
  const posts = [];
  const view = {
    status: { paused: false, enabled: true, learning_enabled: true },
    persona: { id: "可可子", core: "原核心", versions: [] },
    profile: {}, memories: [], scopes: [], styles: [], style_versions: [],
  };
  function element() {
    return {
      value: "", dataset: {}, disabled: false, children: [], handlers: {},
      classList: { contains: () => false, toggle() {} },
      setAttribute() {}, removeAttribute() {}, focus() {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      showModal() { this.open = true; }, close() { this.open = false; },
    };
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
        apiGet: async () => view,
        apiPost: async (endpoint, payload) => {
          posts.push(JSON.parse(JSON.stringify({ endpoint, payload })));
          return { saved: true };
        },
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../pages/manage/app.js"), "utf8"), sandbox);
  return { elements, posts, view, sandbox };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

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
