// Run with `node --test tests/test_webui.cjs` with Playwright available.
const { test, before, after, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

let browser, server, origin, context, page;
const errors = [];
const root = path.resolve(__dirname, "../pages/memoir");

before(async () => {
  server = http.createServer(async (req, res) => {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const file = path.resolve(root, `.${pathname === "/" ? "/index.html" : pathname}`);
    if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
    try {
      const data = await fs.readFile(file);
      res.setHeader("Content-Type", { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }[path.extname(file)] || "application/octet-stream");
      res.end(data);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  await browser?.close();
  if (server) await new Promise((resolve) => server.close(resolve));
});

beforeEach(async () => {
  errors.length = 0;
  context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, reducedMotion: "reduce" });
  await context.addInitScript(() => {
    const now = Math.floor(Date.now() / 1000);
    window.savedRequests = [];
    window.AstrBotPluginPage = {
      ready: async () => ({}),
      onContext: (callback) => { window.changeHostTheme = callback; },
      apiPost: async (endpoint, payload) => {
        window.savedRequests.push({ endpoint, payload });
        return { updated: Object.keys(payload) };
      },
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "browse") {
          const result = await window.AstrBotPluginPage.apiGet(params.kind, params);
          return { ...result, page: params.page || 1, page_size: 20 };
        }
        if (endpoint === "raw/event") return { root: { id: 1, content: "原始事件", source_kind: "forward_root" }, selected_id: params.id, items: [{ id: 3, node_path: "1.2", content: "引用正文", source_meta: JSON.stringify({ name: "Alice", id: "43" }) }] };
        if (endpoint === "overview") return {
          totals: { scopes: 2, memories: 12, raw_turns: 36, pending: 8 },
          scopes: [
            { scope_type: "private", scope_key: "test:小张", memory_count: 8, raw_count: 20, pending_count: 3, last_activity_at: now },
            { scope_type: "group", scope_key: "test:周末摄影小组", memory_count: 4, raw_count: 16, pending_count: 5, last_activity_at: now - 3600 },
          ],
        };
        if (endpoint === "memories") return { total: 3, items: [
          { id: 1, memory_type: "semantic", content: "小张养了一只名叫小福的柴犬，周末喜欢带它去公园散步。", tags: "小福,柴犬,散步", importance: 4, strength: 0.96, updated_at: now },
          { id: 2, memory_type: "insight", content: "聊天时喜欢分享宠物照片，偏好轻松、具体的日常建议。", tags: "宠物,日常", importance: 3, strength: 0.84, updated_at: now - 3600 },
          { id: 3, memory_type: "semantic", content: "正在学习摄影，最近关注自然光和户外人像的拍摄技巧。", tags: "摄影,自然光", importance: 3, strength: 0.72, updated_at: now - 7200 },
        ] };
        if (endpoint === "raw") return { total: 1, items: [{ id: 1, content: "用户: 今天带小福去了公园 [图片] [多媒体解析] 图片1：柴犬坐在树荫下的草地上。 / 助手: 看起来小福玩得很开心！", created_at: now, extracted: false }] };
        if (endpoint === "scope-config") return { override: { recall_top_k: 2 } };
        if (endpoint === "config") return { config: { recall_top_k: 9, background_llm_provider: "vision-model", enable_private_memory: true }, providers: ["vision-model", "audio-model"] };
        if (endpoint === "processing") return { counts: [{ kind: "media", status: "failed", count: 1 }], oldest_pending_seconds: 7200, failures: [{ id: 7, kind: "media", error: "Model timed out", attempts: 1, updated_at: now, retryable: true }] };
        if (endpoint === "memories/sources") return { items: [{ id: 3, content: "Original <script>unsafe()</script> text", speaker_name: "小张" }], tracked: true, expired: 1 };
        if (endpoint === "consents") return { items: [] };
        throw new Error(`Unexpected endpoint: ${endpoint}`);
      },
    };
  });
  page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(origin);
  await page.locator(".mem").first().waitFor();
});

afterEach(async () => {
  await context?.close();
  assert.deepEqual(errors, []);
});

test("manual appearance survives host updates and reloads; auto tracks host", async () => {
  await page.getByRole("radio", { name: "深色", exact: true }).check();
  await page.evaluate(() => window.changeHostTheme({ isDark: false }));
  assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
  await page.reload();
  await page.locator(".mem").first().waitFor();
  assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
  await page.getByRole("radio", { name: "跟随", exact: true }).check();
  await page.evaluate(() => window.changeHostTheme({ isDark: false }));
  assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
  await page.evaluate(() => window.changeHostTheme({ isDark: true }));
  assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
});

test("auto tracks the OS before host context and manual choices sync across tabs", async () => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
  const other = await context.newPage();
  await other.goto(origin);
  await other.locator(".mem").first().waitFor();
  await page.getByRole("radio", { name: "深色", exact: true }).check();
  await other.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  await other.close();
});

test("provider select and global save use global fields, not scope overrides", async () => {
  await page.locator('[data-tab="settings"]').click();
  assert.equal(await page.locator("#scope-recall_top_k").inputValue(), "2");
  await page.locator('[data-view="global"]').click();
  await page.locator("#global-background_llm_provider").selectOption("audio-model");
  await page.locator("#global-recall_top_k").fill("11");
  await page.locator("#save-global-cfg").click();
  const request = await page.evaluate(() => window.savedRequests.at(-1));
  assert.equal(request.endpoint, "config/update");
  assert.equal(request.payload.background_llm_provider, "audio-model");
  assert.equal(request.payload.recall_top_k, 11);
  assert.equal(await page.locator("#scope-recall_top_k").count(), 0);
});

test("theme remains usable when local storage is disabled", async () => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("Blocked", "SecurityError"); } });
  });
  await page.reload();
  await page.getByRole("radio", { name: "深色", exact: true }).check();
  assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
});

test("light, dark and narrow layouts render without horizontal overflow", async () => {
  const output = process.env.MEMOIR_SCREENSHOT_DIR;
  if (output) await fs.mkdir(output, { recursive: true });
  for (const theme of ["浅色", "深色"]) {
    await page.getByRole("radio", { name: theme, exact: true }).check();
    await page.waitForFunction(
      (color) => getComputedStyle(document.body).backgroundColor === color,
      theme === "浅色" ? "rgb(242, 244, 247)" : "rgb(14, 17, 22)",
    );
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    if (output) await page.screenshot({ path: path.join(output, `${theme === "浅色" ? "light" : "dark"}.png`) });
  }
  await page.locator('[data-tab="raw"]').click();
  await page.locator(".raw-event").waitFor();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  if (output) await page.screenshot({ path: path.join(output, "raw.png") });
  await page.setViewportSize({ width: 375, height: 812 });
  for (const tab of ["memories", "raw", "settings"]) {
    await page.locator(`[data-tab="${tab}"]`).click();
    await page.locator(tab === "settings" ? "#scope-config-form" : tab === "raw" ? ".bubble" : ".mem").first().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${tab} overflows`);
  }
  if (output) await page.screenshot({ path: path.join(output, "mobile.png"), fullPage: true });
});

test("late scope responses cannot replace a newer form or change its save target", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint === "scope-config" && params.scope_key === "test:小张") {
        return new Promise((resolve) => { window.releaseOldScope = () => resolve({ override: { recall_top_k: 2 } }); });
      }
      if (endpoint === "scope-config") return { override: { recall_top_k: 9 } };
      return original(endpoint, params);
    };
  });
  await page.locator('[data-tab="settings"]').click();
  await page.waitForFunction(() => typeof window.releaseOldScope === "function");
  await page.locator('[data-scope="group|test:周末摄影小组"]').click();
  await page.waitForFunction(() => document.getElementById("scope-recall_top_k")?.value === "9");
  await page.evaluate(async () => { window.releaseOldScope(); await new Promise(requestAnimationFrame); });
  assert.equal(await page.locator("#scope-recall_top_k").inputValue(), "9");
  await page.locator("#save-scope-cfg").click();
  const saved = await page.evaluate(() => window.savedRequests.at(-1));
  assert.equal(saved.payload.scope_key, "test:周末摄影小组");
  assert.equal(saved.payload.override.recall_top_k, 9);
});

test("source text is escaped and retries remain bound to the displayed scope", async () => {
  await page.locator("[data-sources]").first().click();
  await page.locator("#source-body").first().getByText("Original <script>unsafe()</script> text", { exact: false }).waitFor();
  assert.equal(await page.locator("#source-body script").count(), 0);
  await page.locator('#detail-dialog button').filter({ hasText: "关闭" }).click();
  await page.locator('[data-tab="processing"]').click();
  await page.locator('[data-retry-work="7"]').click();
  const saved = await page.evaluate(() => window.savedRequests.at(-1));
  assert.deepEqual(saved, { endpoint: "processing/retry", payload: { id: 7, scope_type: "private", scope_key: "test:小张" } });
});

test("late memory results cannot overwrite another tab", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === "memories"
      ? new Promise((resolve) => { window.releaseMemories = () => resolve({ items: [], total: 0 }); })
      : original(endpoint, params);
  });
  await page.locator('[data-tab="memories"]').click();
  await page.waitForFunction(() => typeof window.releaseMemories === "function");
  await page.locator('[data-tab="raw"]').click();
  await page.locator(".bubble").first().waitFor();
  await page.evaluate(async () => { window.releaseMemories(); await new Promise(requestAnimationFrame); });
  assert.equal(await page.locator(".bubble").count(), 1);
});

test("forward source trees escape content and unsupported work cannot retry", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint === "raw/event") return { root: { id: 40, content: "转发事件" }, selected_id: 42, items: [{ id: 42, node_path: "1.2.3", content: "<img src=x onerror=unsafe()>", source_meta: JSON.stringify({ name: "<script>unsafe()</script>", id: "43" }) }] };
      if (endpoint === "processing") return { counts: [{ kind: "forward", status: "failed", count: 1 }], oldest_pending_seconds: 0, items: [{ id: 8, kind: "forward", status: "failed", error: "Adapter exposes preview only", retryable: false, attempts: 1 }] };
      return original(endpoint, params);
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('[data-open-event]').first().click();
  await page.locator('#source-body').getByText("节点 1.2.3", { exact: true }).waitFor();
  assert.equal(await page.locator("#source-body script, #source-body img").count(), 0);
  await page.setViewportSize({ width: 375, height: 812 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (process.env.MEMOIR_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.MEMOIR_SCREENSHOT_DIR, "forward-mobile.png"), fullPage: true });
  await page.locator('#detail-dialog button').filter({ hasText: "关闭" }).click();
  await page.locator('[data-tab="processing"]').click();
  await page.getByText("转发解析 #8", { exact: true }).waitFor();
  assert.equal(await page.locator('[data-retry-work="8"]').isDisabled(), true);
});

test("dirty settings block navigation until explicitly discarded", async () => {
  await page.locator('[data-tab="settings"]').click();
  await page.locator('#scope-recall_top_k').fill('7');
  await page.getByText('有未保存修改', { exact: true }).waitFor();
  await page.locator('[data-tab="raw"]').click();
  await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
  await page.locator('#confirm-dialog button[value="cancel"]').click();
  assert.equal(await page.locator('#scope-recall_top_k').inputValue(), '7');
  await page.locator('[data-view="global"]').click();
  await page.locator('#confirm-dialog button[value="accept"]').click();
  await page.locator('#global-config-form').waitFor();
  assert.equal((await page.evaluate(() => window.savedRequests)).length, 0);
});

test("manual edits submit only editable fields and keep failed edits open", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiPost;
    window.rejectEdit = true;
    window.AstrBotPluginPage.apiPost = async (endpoint, payload) => {
      if (endpoint === 'memories/update' && window.rejectEdit) throw new Error('记忆已变化，请刷新');
      return original(endpoint, payload);
    };
  });
  await page.locator('[data-edit-memory="1"]').click();
  await page.locator('#edit-content').fill('人工修改正文');
  await page.locator('#editor-dialog button[type="submit"]').click();
  await page.locator('.toast.err').waitFor();
  assert.equal(await page.locator('#editor-dialog').isVisible(), true);
  assert.equal(await page.locator('#edit-content').inputValue(), '人工修改正文');
  await page.evaluate(() => { window.rejectEdit = false; });
  await page.locator('#editor-dialog button[type="submit"]').click();
  await page.locator('#editor-dialog').waitFor({ state: 'hidden' });
  const saved = await page.evaluate(() => window.savedRequests.at(-1));
  assert.deepEqual(Object.keys(saved.payload).sort(), ['content','id','importance','revision','scope_key','scope_type','tags']);
  assert.equal(saved.payload.scope_key, 'test:小张');
  assert.equal(saved.payload.content, '人工修改正文');
});

test("batch deletion requires confirmation and sends only selected page IDs", async () => {
  await page.locator('#toggle-selection').click();
  await page.locator('#select-page').check();
  await page.locator('#delete-selection').click();
  await page.locator('#confirm-dialog button[value="cancel"]').click();
  assert.equal((await page.evaluate(() => window.savedRequests)).length, 0);
  await page.locator('[data-select="2"]').uncheck();
  await page.locator('#delete-selection').click();
  await page.locator('#confirm-dialog button[value="accept"]').click();
  await page.waitForFunction(() => window.savedRequests.length === 1);
  const saved = await page.evaluate(() => window.savedRequests[0]);
  assert.deepEqual(saved, { endpoint: 'records/delete', payload: { scope_type: 'private', scope_key: 'test:小张', kind: 'memories', ids: [1,3] } });
});

test("raw filters and search paginate server-side and discard stale search responses", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.browseRequests = [];
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint !== 'browse') return original(endpoint, params);
      window.browseRequests.push(params);
      if (params.q === '旧搜索') return new Promise(resolve => { window.releaseOldSearch = () => resolve({ items: [{ id: 99, content: '过时结果', created_at: 1234 }], total: 1, page: 1 }); });
      return { items: [{ id: 30, content: params.q || '新结果', created_at: 1234 }], total: 45, page: params.page, page_size: 20 };
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('#search').fill('旧搜索');
  await page.waitForFunction(() => typeof window.releaseOldSearch === 'function');
  await page.locator('#search').fill('新搜索');
  await page.locator('.bubble').getByText('新搜索', { exact: true }).waitFor();
  await page.evaluate(() => window.releaseOldSearch());
  assert.equal(await page.getByText('过时结果', { exact: true }).count(), 0);
  await page.locator('[data-filter="source"]').selectOption('forwarded');
  await page.locator('[data-filter="since"]').fill('2026-09-01');
  await page.locator('[data-filter="since"]').dispatchEvent('change');
  await page.locator('#next').click();
  await page.waitForFunction(() => window.browseRequests.at(-1).page === 2);
  const query = await page.evaluate(() => window.browseRequests.at(-1));
  assert.equal(query.kind, 'raw'); assert.equal(query.q, '新搜索'); assert.equal(query.source, 'forwarded'); assert.equal(typeof query.since, 'number');
});

test("global settings remain accessible without scopes and mobile drawer closes after selection", async () => {
  await page.setViewportSize({ width: 375, height: 812 });
  assert.equal(await page.locator('#scope-sidebar').isVisible(), false);
  await page.locator('#scope-toggle').click();
  await page.locator('[data-scope="group|test:周末摄影小组"]').click();
  await page.waitForFunction(() => document.getElementById('mobile-title').textContent.includes('周末摄影小组'));
  assert.equal(await page.locator('#scope-toggle').getAttribute('aria-expanded'), 'false');
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'overview' ? Promise.resolve({ scopes: [], totals: { scopes: 0, memories: 0, raw_turns: 0, pending: 0 } }) : original(endpoint, params);
  });
  await page.locator('#scope-toggle').click();
  await page.locator('#refresh').click();
  await page.getByText('还没有会话', { exact: true }).waitFor();
  await page.locator('[data-view="global"]').click();
  await page.locator('#global-config-form').waitFor();
  assert.equal(await page.locator('#scope-sidebar').isVisible(), false);
});

test("task polling stops on navigation and late task responses cannot replace the page", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.processingCalls = 0;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint !== 'processing') return original(endpoint, params);
      window.processingCalls++;
      return { counts: [{ kind: 'forward', status: 'running', count: 1 }], items: [], oldest_pending_seconds: 0 };
    };
  });
  await page.locator('[data-tab="processing"]').click();
  await page.waitForFunction(() => window.processingCalls === 1);
  await page.locator('[data-tab="memories"]').click();
  await page.locator('.mem').first().waitFor();
  await page.waitForTimeout(5200);
  assert.equal(await page.evaluate(() => window.processingCalls), 1);
});
