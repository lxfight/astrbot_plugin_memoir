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
  context = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce" });
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
      apiGet: async (endpoint) => {
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
  await page.locator("#global-background_llm_provider").selectOption("audio-model");
  await page.locator("#global-recall_top_k").fill("11");
  await page.locator("#save-global-cfg").click();
  const request = await page.evaluate(() => window.savedRequests.at(-1));
  assert.equal(request.endpoint, "config/update");
  assert.equal(request.payload.background_llm_provider, "audio-model");
  assert.equal(request.payload.recall_top_k, 11);
  assert.equal(await page.locator("#scope-recall_top_k").inputValue(), "2");
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
  await page.locator(".media-description").waitFor();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  if (output) await page.screenshot({ path: path.join(output, "raw.png") });
  await page.setViewportSize({ width: 375, height: 812 });
  for (const tab of ["memories", "raw", "settings"]) {
    await page.locator(`[data-tab="${tab}"]`).click();
    await page.locator(tab === "settings" ? "#global-config-form" : tab === "raw" ? ".bubble" : ".mem").first().waitFor();
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
  await page.locator(".memory-sources summary").first().click();
  await page.locator(".source-content").first().getByText("Original <script>unsafe()</script> text", { exact: false }).waitFor();
  assert.equal(await page.locator(".source-content script").count(), 0);
  await page.locator('[data-tab="settings"]').click();
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
  assert.equal(await page.locator(".bubble").count(), 2);
});

test("forward provenance is escaped, visible on mobile, and unsupported work cannot retry", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint === "raw") return { total: 1, items: [{ id: 42, parent_id: 40, source_kind: "forwarded", content: "转发正文", extracted: 0, created_at: 1234, source_meta: JSON.stringify({ status: "partial", path: "1.2.3", name: "<script>unsafe()</script>", id: "43", problems: ["<img src=x onerror=unsafe()>"] }) }] };
      if (endpoint === "processing") return { counts: [{ kind: "forward", status: "failed", count: 1 }], oldest_pending_seconds: 0, failures: [{ id: 8, kind: "forward", error: "Adapter exposes preview only", retryable: false, attempts: 1 }] };
      return original(endpoint, params);
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.getByText("转发引用 · 部分解析 · 来源 #40").waitFor();
  await page.getByText("来源详情", { exact: true }).click();
  await page.getByText("节点 1.2.3", { exact: false }).waitFor();
  assert.equal(await page.locator(".chat script, .chat img").count(), 0);
  await page.setViewportSize({ width: 375, height: 812 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (process.env.MEMOIR_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.MEMOIR_SCREENSHOT_DIR, "forward-mobile.png"), fullPage: true });
  await page.locator('[data-tab="settings"]').click();
  await page.getByText("转发解析 #8", { exact: true }).waitFor();
  assert.equal(await page.locator('[data-retry-work="8"]').isDisabled(), true);
});
