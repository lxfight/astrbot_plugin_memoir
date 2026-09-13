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
        if (endpoint === "scope-config") return { override: { recall_top_k: 2, recall_recent_turns: 5 }, effective: { recall_recent_turns: params.scope_type === "group" ? 0 : 5 }, compatibility: { enabled: true, detected: true, audio: true, group_image: params.scope_type === "group", request_image: params.scope_type !== "group", recent: params.scope_type === "group" } };
        if (endpoint === "config") return { config: { auto_native_compatibility: true, recall_top_k: 9, background_llm_provider: "vision-model", enable_private_memory: true }, providers: ["vision-model", "audio-model"] };
        if (endpoint === "processing") return { counts: [{ kind: "media", status: "failed", count: 1 }], oldest_pending_seconds: 7200, failures: [{ id: 7, kind: "media", error: "Model timed out", attempts: 1, updated_at: now, retryable: true }] };
        if (endpoint === "memories/sources") return { items: [{ id: 3, content: "Original <script>unsafe()</script> text", speaker_name: "小张" }], tracked: true, expired: 1 };
        if (endpoint === "consents") return { items: [] };
        if (endpoint === "usage") {
          window.usageRequests ??= []; window.usageRequests.push(params);
          const offset = Number(params.offset || 0), until = now + 1;
          const since = (Math.floor((now + offset * 60) / 86400) - Number(params.days || 30) + 1) * 86400 - offset * 60;
          const totals = { calls: 4, reported_calls: 3, failed_calls: 1, input_other: 12345, input_cached: 6000, output: 789 };
          const daily = Array.from({ length: 7 }, (_, index) => ({ ...totals, day: new Date((until + offset * 60 - (6 - index) * 86400) * 1000).toISOString().slice(0, 10), input_other: 1500 + index * 50, input_cached: 600 + index * 50, output: 100 + index * 3 }));
          return { since, until, offset, totals, daily, provider_id: [{ ...totals, provider_id: "vision-model" }], purpose: [{ ...totals, purpose: "media_image" }], items: [
            { id: params.before ? 2 : 52, created_at: now, scope_type: "private", scope_key: "test:小张", provider_id: "vision-model", model: "vision-v2", purpose: "media_image", input_other: 12345, input_cached: 6000, output: 789, duration_ms: 1530, status: "complete" },
            { id: params.before ? 1 : 51, created_at: now - 3600, scope_type: "group", scope_key: "test:周末摄影小组", provider_id: "audio-model", model: "audio-v1", purpose: "media_audio", input_other: null, input_cached: null, output: null, duration_ms: 30000, status: "error", error_type: "TimeoutError" },
          ], next_cursor: params.before ? null : 51 };
        }
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

test("usage shows exact cache-aware totals, unknown calls and bounded detail pages", async () => {
  await page.locator('[data-view="usage"]').click();
  assert.equal(await page.locator(".usage-primary strong").textContent(), "19,134");
  assert.equal(await page.locator(".usage-kpi").last().locator("strong").textContent(), "1");
  assert.match(await page.locator(".usage-details tbody tr").last().textContent(), /未知/);
  await page.locator(".usage-chart [data-day]").last().focus();
  assert.match(await page.locator("#usage-day").textContent(), /缓存输入 900/);
  await page.locator("#usage-next").click();
  await page.waitForFunction(() => window.usageRequests.at(-1).before === 51);
  assert.equal(await page.locator(".usage-details tbody tr").count(), 2);
  assert.equal(await page.locator("#usage-next").isDisabled(), true);
  await page.locator("#usage-prev").click();
  await page.locator('[data-filter="days"]').selectOption("7");
  await page.locator('[data-filter="usage_scope"]').selectOption("group|test:周末摄影小组");
  await page.locator('[data-filter="provider"]').selectOption("vision-model");
  await page.locator('[data-filter="purpose"]').selectOption("media_image");
  const params = await page.evaluate(() => window.usageRequests.at(-1));
  assert.equal(params.days, "7"); assert.equal(params.scope_type, "group");
  assert.equal(params.scope_key, "test:周末摄影小组"); assert.equal(params.provider, "vision-model");
  assert.equal(params.purpose, "media_image"); assert.equal(params.before, 0);
});

test("usage works without scopes, ignores late responses and can retry failures", async () => {
  await page.evaluate(() => {
    const apiGet = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint === "overview") return { totals: { scopes: 0, memories: 0, raw_turns: 0, pending: 0 }, scopes: [] };
      if (endpoint === "usage" && window.delayUsage) await new Promise(resolve => { window.finishUsage = resolve; });
      if (endpoint === "usage" && window.failUsage) throw new Error("Offline");
      return apiGet(endpoint, params);
    };
  });
  await page.locator("#refresh").click();
  await page.locator('[data-view="usage"]').click();
  await page.locator(".usage-page").waitFor();
  await page.evaluate(() => { window.delayUsage = true; });
  await page.locator('[data-filter="days"]').selectOption("7");
  await page.waitForFunction(() => Boolean(window.finishUsage));
  await page.locator('[data-view="global"]').click();
  await page.locator("#global-image_llm_provider").waitFor();
  await page.evaluate(() => { window.delayUsage = false; window.finishUsage(); });
  await page.waitForTimeout(50);
  assert.equal(await page.locator(".usage-page").count(), 0);
  await page.evaluate(() => { window.failUsage = true; });
  await page.locator('[data-view="usage"]').click();
  await page.locator('[data-retry="usage"]').waitFor();
  await page.evaluate(() => { window.failUsage = false; });
  await page.locator('[data-retry="usage"]').click();
  await page.locator(".usage-page").waitFor();
});

test("usage dashboard renders at 1080p in both themes and fits mobile", async () => {
  await page.locator('[data-view="usage"]').click();
  const output = process.env.MEMOIR_SCREENSHOT_DIR;
  if (output) await fs.mkdir(output, { recursive: true });
  for (const [label, theme] of [["浅色", "light"], ["深色", "dark"]]) {
    await page.getByRole("radio", { name: label, exact: true }).check();
    await page.waitForFunction(expected => document.documentElement.dataset.theme === expected, theme);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    if (output) await page.screenshot({ path: path.join(output, `usage-${theme}.png`) });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (output) await page.screenshot({ path: path.join(output, "usage-mobile.png"), fullPage: true });
});

test("native compatibility shows scope-specific ownership without erasing overrides", async () => {
  await page.locator('[data-tab="settings"]').click();
  assert.match(await page.locator(".native-compatibility").textContent(), /原生 STT 已开启，插件停用/);
  assert.match(await page.locator(".native-compatibility").textContent(), /触发主对话时/);
  await page.locator('[data-scope="group|test:周末摄影小组"]').click();
  assert.match(await page.locator(".native-compatibility").textContent(), /原生群聊图片转述接管/);
  const recent = page.locator(".field").filter({ has: page.locator("#scope-recall_recent_turns") });
  assert.equal(await page.locator("#scope-recall_recent_turns").inputValue(), "5");
  assert.match(await recent.textContent(), /当前生效：0 · 原生兼容策略/);
  const output = process.env.MEMOIR_SCREENSHOT_DIR;
  if (output) await fs.mkdir(output, { recursive: true });
  for (const [label, theme] of [["浅色", "light"], ["深色", "dark"]]) {
    await page.getByRole("radio", { name: label, exact: true }).check();
    await page.waitForFunction(expected => document.documentElement.dataset.theme === expected, theme);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    if (output) await page.screenshot({ path: path.join(output, `native-${theme}.png`) });
  }
  await page.locator('[data-view="global"]').click();
  assert.equal(await page.locator("#global-auto_native_compatibility").isChecked(), true);
  const provider = await page.locator("#global-background_llm_provider").inputValue();
  await page.locator("label.switch").filter({ has: page.locator("#global-auto_native_compatibility") }).click();
  assert.equal(await page.locator("#global-auto_native_compatibility").isChecked(), false);
  await page.locator("#save-global-cfg").click();
  const request = await page.evaluate(() => window.savedRequests.at(-1));
  assert.equal(request.payload.auto_native_compatibility, false);
  assert.equal(request.payload.background_llm_provider, provider);
  await page.locator('[data-scope="group|test:周末摄影小组"]').click();
  await page.locator('[data-tab="settings"]').click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (output) await page.screenshot({ path: path.join(output, "native-mobile.png"), fullPage: true });
});

test("native compatibility marks missing detection and an explicitly disabled switch", async () => {
  await page.evaluate(() => {
    const apiGet = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      const result = await apiGet(endpoint, params);
      if (endpoint === "scope-config") result.compatibility = { enabled: !window.nativeDisabled, detected: false };
      return result;
    };
  });
  await page.locator('[data-tab="settings"]').click();
  assert.match(await page.locator(".native-compatibility").textContent(), /暂时无法检测/);
  await page.evaluate(() => { window.nativeDisabled = true; });
  await page.locator("#refresh").click();
  await page.waitForFunction(() => document.querySelector(".native-compatibility")?.textContent.includes("自动兼容已关闭"));
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
  await page.locator("#global-image_llm_provider").selectOption("vision-model");
  await page.locator("#global-audio_llm_provider").selectOption("audio-model");
  await page.locator("#global-recall_top_k").fill("11");
  await page.locator("#save-global-cfg").click();
  const request = await page.evaluate(() => window.savedRequests.at(-1));
  assert.equal(request.endpoint, "config/update");
  assert.equal(request.payload.background_llm_provider, "audio-model");
  assert.equal(request.payload.image_llm_provider, "vision-model");
  assert.equal(request.payload.audio_llm_provider, "audio-model");
  assert.equal(await page.locator("#global-video_llm_provider").count(), 0);
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
  await page.locator('#confirm-dialog button[value="accept"]').click();
  await page.waitForFunction(() => window.savedRequests.some(r => r.endpoint === "processing/retry"));
  const saved = await page.evaluate(() => window.savedRequests.at(-1));
  assert.deepEqual(saved, { endpoint: "processing/retry", payload: { id: 7, scope_type: "private", scope_key: "test:小张", attachments: [] } });
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
  assert.equal(await page.locator(".raw-event").count(), 1);
  assert.equal(await page.locator(".bubble").count(), 2);
});

test("raw conversations keep role bubbles, quoted identity and event actions across layouts", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    const now = Math.floor(Date.now() / 1000);
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint === "raw") return { total: 3, items: [
        { id: 1, source_kind: "native", content: "用户: 今天带小福去了公园 [图片] / 助手: 小福看起来玩得很开心！树荫下的光线也很适合拍照。", created_at: now, processing_status: "unextracted" },
        { id: 2, source_kind: "native", speaker_name: "小林", speaker_id: "photographer-02", content: "周末的摄影活动定在植物园吧，我带反光板，大家可以练习自然光人像。", created_at: now - 600, processing_status: "complete" },
        { id: 3, source_kind: "forward_root", speaker_name: "小张", speaker_id: "42", chunk_count: 8, content: "用户: 引用内容 / 助手: 引用回复", created_at: now - 1200, processing_status: "partial" },
      ] };
      return original(endpoint, params);
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('.forward-preview').waitFor();
  assert.equal(await page.locator('.msg.me .record-text').textContent(), '今天带小福去了公园 [图片]');
  assert.match(await page.locator('.msg.bot .record-text').textContent(), /小福看起来/);
  assert.equal(await page.locator('.raw-event:not(.forward-event) .msg.other .msg-head').textContent(), '小林');
  assert.equal(await page.locator('.forward-event .msg.me, .forward-event .msg.bot').count(), 0);
  assert.match(await page.locator('.forward-note').textContent(), /未经验证/);
  assert.equal(await page.locator('[data-del-raw]').count(), 3);
  const menu = page.locator('.message-menu').first();
  await menu.locator('summary').click();
  assert.equal(await menu.locator('[data-del-raw]').isVisible(), true);
  await page.keyboard.press('Escape');
  assert.equal(await menu.evaluate(el => el.open), false);
  await menu.locator('summary').click();
  await page.locator('.day-chip').click();
  assert.equal(await menu.evaluate(el => el.open), false);
  const output = process.env.MEMOIR_SCREENSHOT_DIR;
  for (const theme of ['浅色', '深色']) {
    await page.getByRole('radio', { name: theme, exact: true }).check();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (output) await page.screenshot({ path: path.join(output, `raw-${theme === '浅色' ? 'light' : 'dark'}.png`) });
  }
  await page.setViewportSize({ width: 375, height: 812 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (output) await page.screenshot({ path: path.join(output, 'raw-mobile.png'), fullPage: true });
  await page.locator('#toggle-selection').click();
  await page.locator('[data-select="1"]').check();
  assert.match(await page.locator('#selection-bar').textContent(), /已选 1 条/);
  await page.locator('.message-menu').filter({ has: page.locator('[data-del-raw="1"]') }).locator('summary').click();
  const menuBox = await page.locator('.message-menu[open] .message-menu-panel').boundingBox();
  assert.ok(menuBox.x >= 0 && menuBox.x + menuBox.width <= 375);
  await page.locator('[data-del-raw="1"]').click();
  assert.equal(await page.locator('#confirm-dialog').evaluate(el => el.open), true);
  await page.locator('#confirm-dialog').getByRole('button', { name: '取消', exact: true }).click();
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => endpoint === 'raw'
      ? { total: 2, items: [
        { id: 4, source_kind: 'forward_node', content: '用户: <img src=x onerror=unsafe()> / 助手: ' + '引用文本'.repeat(120), created_at: Math.floor(Date.now() / 1000) },
        { id: 5, source_kind: 'native', content: '助手: 群聊中的独立回复', created_at: Math.floor(Date.now() / 1000) },
      ] }
      : original(endpoint, params);
  });
  await page.locator('#clear-filters').click();
  await page.locator('.long-text').waitFor();
  assert.equal(await page.locator('.raw-event').first().locator('.msg.me, .msg.bot, .bubble img').count(), 0);
  assert.equal(await page.locator('.msg.bot .record-text').textContent(), '群聊中的独立回复');
  await page.locator('.long-text summary').click();
  assert.match(await page.locator('.long-text p').textContent(), /<img src=x onerror=unsafe\(\)>/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
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
  await page.locator('.message-menu').first().locator('summary').click();
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

test("raw filters and search use cursors and discard stale search responses", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.browseRequests = [];
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint !== 'browse') return original(endpoint, params);
      window.browseRequests.push(params);
      if (params.q === '旧搜索') return new Promise(resolve => { window.releaseOldSearch = () => resolve({ items: [{ id: 99, content: '过时结果', created_at: 1234 }], total: 1, page: 1 }); });
      return { items: [{ id: params.before ? 29 : 30, content: params.q || '新结果', created_at: 1234 }], has_more: !params.before, next_cursor: params.before ? null : '1234:30' };
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('#search').fill('旧搜索');
  await page.waitForFunction(() => typeof window.releaseOldSearch === 'function');
  await page.locator('#search').fill('新搜索');
  await page.locator('.bubble').getByText('新搜索', { exact: true }).first().waitFor();
  await page.evaluate(() => window.releaseOldSearch());
  assert.equal(await page.getByText('过时结果', { exact: true }).count(), 0);
  await page.locator('[data-filter="source"]').selectOption('forwarded');
  await page.locator('[data-filter="since"]').fill('2026-09-01');
  await page.locator('[data-filter="since"]').dispatchEvent('change');
  await page.waitForFunction(() => window.browseRequests.at(-1).before === '1234:30');
  assert.equal(await page.locator('#pager').isVisible(), false);
  const query = await page.evaluate(() => window.browseRequests.at(-1));
  assert.equal(query.kind, 'raw'); assert.equal(query.q, '新搜索'); assert.equal(query.source, 'forwarded'); assert.equal(typeof query.since, 'number');
});

test("raw history anchors prepends and bounds DOM while retaining expanded and selected records", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.historyRequests = [];
    window.holdOlder = true;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint !== 'browse' || params.kind !== 'raw') return original(endpoint, params);
      window.historyRequests.push(params);
      const last = params.before ? Number(params.before.split(':')[1]) - 1 : 600;
      const first = Math.max(1, last - 39);
      const result = { items: Array.from({ length: last - first + 1 }, (_, i) => ({ id: last - i, created_at: 1234, content: last - i === 600 ? '很长的消息正文'.repeat(90) : `历史消息 ${last - i}`, source_kind: 'native' })), has_more: first > 1, next_cursor: first > 1 ? `1234:${first}` : null };
      if (params.before && window.holdOlder) return new Promise(resolve => { window.releaseOlder = () => { window.holdOlder = false; resolve(result); }; });
      return result;
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('[data-event-id="600"]').waitFor();
  assert.equal(await page.locator('#pager').isVisible(), false);
  await page.waitForFunction(() => { const el = document.getElementById('content'); return el.scrollHeight - el.scrollTop - el.clientHeight < 3; });
  await page.locator('#content').evaluate(el => { el.scrollTop = 100; });
  await page.waitForFunction(() => typeof window.releaseOlder === 'function');
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const anchor = await page.evaluate(() => {
    const top = document.getElementById('content').getBoundingClientRect().top;
    const row = [...document.querySelectorAll('.history-row')].find(el => el.getBoundingClientRect().bottom > top);
    return { id: row.dataset.eventId, y: row.getBoundingClientRect().top };
  });
  await page.locator('#content').evaluate(el => { el.dispatchEvent(new Event('scroll')); el.dispatchEvent(new Event('scroll')); });
  assert.equal(await page.evaluate(() => window.historyRequests.length), 2);
  await page.evaluate(() => window.releaseOlder());
  await page.waitForFunction(() => document.getElementById('result-count').textContent.includes('已加载 80 '));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const after = await page.locator(`[data-event-id="${anchor.id}"]`).boundingBox();
  assert.ok(Math.abs(after.y - anchor.y) < 3, `prepend moved anchor by ${after.y - anchor.y}px`);
  for (let count = 120; count <= 600; count += 40) {
    await page.locator('#content').evaluate(el => { el.scrollTop = 0; });
    await page.waitForFunction(n => document.getElementById('result-count').textContent.includes(`已加载 ${n} `), count);
    assert.ok(await page.locator('.history-row').count() <= 60);
  }
  assert.equal(await page.evaluate(() => window.historyRequests.length), 15);
  await page.locator('#content').evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.locator('[data-event-id="600"] .long-text summary').click();
  await page.locator('#toggle-selection').click();
  await page.locator('[data-select="600"]').check();
  await page.locator('#content').evaluate(el => { el.scrollTop = 0; });
  await page.locator('[data-event-id="1"]').waitFor();
  await page.locator('#content').evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.locator('[data-event-id="600"]').waitFor();
  assert.equal(await page.locator('[data-select="600"]').isChecked(), true);
  assert.equal(await page.locator('[data-event-id="600"] .long-text').evaluate(el => el.open), true);
  await page.locator('#select-page').check();
  assert.match(await page.locator('#selection-bar').textContent(), /已选 100 条/);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
  assert.ok(await page.locator('.history-row').count() <= 60);
});

test("failed history loading retries and late scope results are discarded", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.failHistory = true;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      if (endpoint !== 'browse' || params.kind !== 'raw') return original(endpoint, params);
      if (params.before && window.failHistory) { window.failHistory = false; throw new Error('Network unavailable'); }
      if (params.before) return new Promise(resolve => { window.releaseHistory = () => resolve({ items: [{ id: 1, content: '旧会话迟到消息', created_at: 1234 }], has_more: false }); });
      return { items: Array.from({ length: 40 }, (_, i) => ({ id: 80 - i, created_at: 1234, content: `消息 ${80 - i}` })), has_more: true, next_cursor: '1234:41' };
    };
  });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('[data-event-id="80"]').waitFor();
  await page.locator('#content').evaluate(el => { el.scrollTop = 0; });
  await page.getByRole('button', { name: '加载失败 · 点击重试', exact: true }).click();
  await page.waitForFunction(() => typeof window.releaseHistory === 'function');
  await page.locator('[data-tab="memories"]').click();
  await page.locator('.mem').first().waitFor();
  await page.evaluate(() => window.releaseHistory());
  assert.equal(await page.getByText('旧会话迟到消息', { exact: true }).count(), 0);
  assert.equal(await page.locator('.mem').count(), 3);
});

test("mobile history opens at the latest message and scrolls inside the chat", async () => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.locator('[data-tab="raw"]').click();
  await page.locator('.history-row').waitFor();
  await page.waitForFunction(() => { const el = document.getElementById('content'); return el.scrollHeight - el.scrollTop - el.clientHeight < 3; });
  assert.equal(await page.locator('#content').evaluate(el => getComputedStyle(el).overflowY), 'auto');
  assert.equal(await page.locator('.msg.bot').isVisible(), true);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  if (process.env.MEMOIR_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.MEMOIR_SCREENSHOT_DIR, 'raw-mobile.png') });
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

test("media presets preserve budgets and model selections, render at 1080p and mobile", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
      const result = await original(endpoint, params);
      if (endpoint === "config") {
        result.config.image_llm_provider = "vision-model";
        result.config.media_daily_requests = 30;
        result.media_budget = { offset: 480, reset_at: Math.floor(Date.now()/1000)+3600, buckets: [{ scope: null, usage: [{kind: "image",requests: 8,images: 10,seconds: 0,tokens: 5400,unknown: 1}], limits: {media_daily_requests:30,media_daily_tokens:50000,image_daily_count:60,audio_daily_seconds:300}, unresolved:1 }], decisions:[{reason:"cache_hit",count:4}] };
      }
      return result;
    };
  });
  await page.locator('[data-view="global"]').click();
  await page.locator('[data-media-preset="saving"]').click();
  assert.equal(await page.locator('#global-image_forward_mode').inputValue(), 'manual');
  assert.equal(await page.locator('#global-media_daily_requests').inputValue(), '30');
  assert.equal(await page.locator('#global-image_llm_provider').inputValue(), 'vision-model');
  assert.match(await page.locator('.media-budget').textContent(), /剩余 22/);
  assert.match(await page.locator('.media-budget').textContent(), /缓存复用 4 次/);
  assert.match(await page.locator('.save-state').textContent(), /未保存/);
  const output = process.env.MEMOIR_SCREENSHOT_DIR;
  for (const [label, theme] of [["浅色","light"],["深色","dark"]]) {
    await page.getByRole("radio", {name:label,exact:true}).check();
    await page.locator('[data-media-preset="saving"]').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (output) await page.screenshot({path:path.join(output,`cost-${theme}.png`)});
  }
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (output) await page.screenshot({path:path.join(output,'cost-mobile.png')});
  await page.locator('#save-global-cfg').click();
  const saved = await page.evaluate(() => window.savedRequests.at(-1));
  assert.equal(saved.payload.media_cache_days,7);
  assert.equal(saved.payload.media_daily_requests,30);
  assert.equal(saved.payload.image_llm_provider,'vision-model');
});

test("manual media preview submits only checked attachments and keeps its scope", async () => {
  await page.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.AstrBotPluginPage.apiGet = async (endpoint, params) => endpoint === 'processing' ? {counts:[],oldest_pending_seconds:0,items:[{id:12,kind:'media',status:'skipped',error:'policy: manual only',retryable:true,attachments:[{index:1,kind:'image'},{index:2,kind:'audio'}]}]} : original(endpoint,params);
  });
  await page.locator('[data-tab="processing"]').click();
  await page.locator('[data-retry-work="12"]').click();
  await page.locator('#confirm-dialog input[value="1"]').uncheck();
  await page.locator('#confirm-dialog button[value="accept"]').click();
  await page.waitForFunction(() => window.savedRequests.some(r => r.endpoint === 'processing/retry'));
  assert.deepEqual(await page.evaluate(() => window.savedRequests.at(-1)), {endpoint:'processing/retry',payload:{id:12,scope_type:'private',scope_key:'test:小张',attachments:[2]}});
});
