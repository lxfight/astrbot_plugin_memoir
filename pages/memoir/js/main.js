import { $, RETRY_LOADERS, sameScope, state } from "./state.js";
import { esc, emptyState, refreshIcons, toast } from "./utils.js";
import { bridge, safe } from "./api.js";
import { initTheme } from "./theme.js";
import { renderOverview, renderSidebar, renderDetailHead } from "./sidebar.js";
import { loadMemories } from "./memories.js";
import { loadRaw } from "./raw.js";
import { renderFilters, renderSelection } from "./list.js";
import { ask, canLeave, isDirty, openEditor, openSources, openRawEvent } from "./dialogs.js";
import { loadScopeConfigTab, resetScopeConfig, loadConsentPage, loadProcessingPage, loadProcessingStatus, stopProcessing } from "./settings.js";

const globalView = () => ["global", "consents"].includes(state.tab);
const viewKey = () => JSON.stringify([state.scope?.scope_type, state.scope?.scope_key, state.tab]);
let navigating = false;

async function loadTab() {
  stopProcessing();
  const records = ["memories", "raw"].includes(state.tab);
  $("search-wrap").style.display = records && state.scope ? "flex" : "none";
  $("pager").style.display = "none";
  $("selection-bar").hidden = true;
  $("filters").innerHTML = "";
  $("tabs").hidden = globalView();
  document.querySelectorAll("[data-tab]").forEach(button => {
    const active = button.dataset.tab === state.tab;
    button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === state.tab));
  renderDetailHead();
  if (!state.scope && !globalView()) {
    state.loadVersion++;
    $("content").innerHTML = emptyState("inbox", "与机器人对话后，会话会出现在这里。也可以先打开全局设置调整记忆开关。", "还没有会话");
  } else if (records) {
    renderFilters();
    await (state.tab === "memories" ? loadMemories() : loadRaw());
  } else if (state.tab === "processing") {
    $("filters").innerHTML = `<label>任务状态<select data-filter="task_status">${[["", "全部状态"], ["failed", "失败"], ["pending", "排队"], ["running", "处理中"], ["complete", "完成"]].map(([value, label]) => `<option value="${value}" ${state.filters.task_status === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>`;
    await loadProcessingPage();
  } else if (state.tab === "consents") await loadConsentPage();
  else await loadScopeConfigTab();
  refreshIcons();
}

async function refreshOverview() {
  const version = ++state.overviewVersion;
  const result = await safe("刷新概览", () => bridge.apiGet("overview"));
  if (!result || version !== state.overviewVersion) return false;
  state.scopes = result.scopes || [];
  const current = state.scopes.find(scope => sameScope(scope, state.scope)) || state.scopes[0];
  state.scope = current ? { scope_type: current.scope_type, scope_key: current.scope_key } : null;
  renderOverview(result.totals); renderSidebar(); renderDetailHead();
  return true;
}

function closeDrawer() {
  $("scope-sidebar").classList.remove("drawer-open");
  $("scope-toggle").setAttribute("aria-expanded", "false");
  $("scope-backdrop").hidden = true;
  $("scope-sidebar").inert = matchMedia("(max-width: 700px)").matches;
  document.body.classList.remove("drawer-visible");
  document.querySelector("main").inert = false;
}

async function navigate(tab, scope = state.scope) {
  if (navigating || state.busy) return;
  navigating = true;
  try {
    if (!await canLeave()) return;
    clearTimeout(state.searchTimer);
    state.views.set(viewKey(), { q: state.q, filters: { ...state.filters }, page: state.page });
    state.tab = tab; state.scope = scope;
    const saved = state.views.get(viewKey()) || {};
    state.q = saved.q || ""; state.filters = saved.filters || {}; state.page = saved.page || 1;
    state.scopeOverride = {}; state.selected.clear(); state.selecting = false;
    renderSidebar(); closeDrawer();
    $("content").scrollTop = 0;
    return loadTab();
  } finally { navigating = false; }
}

async function removeRecords(kind, ids) {
  if (state.busy || !ids.length) return;
  state.busy = true;
  const scope = { ...state.scope };
  const forwarded = kind === "raw" && ids.some(id => state.items.find(row => row.id === id)?.source_kind === "forward_root");
  try {
    const message = kind === "memories" ? "只删除选中的记忆，来源原文仍会保留。此操作不可恢复。" : forwarded ? "选中的转发事件将连同全部引用分块和解析任务删除；已提炼的记忆保留。此操作不可恢复。" : "删除所选原始消息及关联解析任务，已提炼的记忆保留。此操作不可恢复。";
    if (!await ask(`删除 ${ids.length} 条${kind === "memories" ? "记忆" : "原始消息"}？`, message, "确认删除", true)) return;
    const result = await safe("删除记录", () => bridge.apiPost("records/delete", { ...scope, kind, ids }));
    if (result === null) return;
    state.selected.clear(); toast(`已删除 ${result.deleted} 条选中记录`);
    await refreshOverview(); await loadTab();
  } finally { state.busy = false; }
}

async function clearScope() {
  if (!state.scope || state.busy || !await canLeave()) return;
  state.busy = true;
  const scope = { ...state.scope };
  try {
    if (!await ask("清空整个会话？", `${scope.scope_key} 的全部记忆、原始消息、转发分块和处理任务都会删除，且无法恢复。`, "清空会话", true)) return;
    const result = await safe("清空会话", () => bridge.apiPost("scope/clear", scope));
    if (result === null) return;
    state.selected.clear(); state.page = 1; toast("会话已清空"); await refreshOverview(); await loadTab();
  } finally { state.busy = false; }
}

function bindEvents() {
  $("tabs").onclick = e => { const tab = e.target.closest("[data-tab]"); if (tab) navigate(tab.dataset.tab); };
  document.querySelector(".global-nav").onclick = e => { const button = e.target.closest("[data-view]"); if (button) navigate(button.dataset.view); };
  $("scope-list").onclick = e => {
    const item = e.target.closest("[data-scope]"); if (!item) return;
    const split = item.dataset.scope.indexOf("|");
    navigate(globalView() ? "memories" : state.tab, { scope_type: item.dataset.scope.slice(0, split), scope_key: item.dataset.scope.slice(split + 1) });
  };
  $("scope-filter").oninput = e => { state.scopeFilter = e.target.value; renderSidebar(); };
  $("scope-toggle").onclick = () => {
    if ($("scope-sidebar").classList.contains("drawer-open")) { closeDrawer(); return; }
    $("scope-sidebar").inert = false; $("scope-sidebar").classList.add("drawer-open");
    $("scope-toggle").setAttribute("aria-expanded", "true"); $("scope-backdrop").hidden = false;
    document.body.classList.add("drawer-visible"); document.querySelector("main").inert = true; $("scope-filter").focus();
  };
  $("scope-backdrop").onclick = () => { closeDrawer(); $("scope-toggle").focus(); };
  document.addEventListener("keydown", e => {
    if (!$("scope-sidebar").classList.contains("drawer-open")) return;
    if (e.key === "Escape") { closeDrawer(); $("scope-toggle").focus(); }
    if (e.key === "Tab") {
      const controls = [...$("scope-sidebar").querySelectorAll("button,input,select")].filter(el => !el.disabled && el.offsetParent !== null);
      if (e.shiftKey && document.activeElement === controls[0]) { e.preventDefault(); controls.at(-1)?.focus(); }
      else if (!e.shiftKey && document.activeElement === controls.at(-1)) { e.preventDefault(); controls[0]?.focus(); }
    }
  });
  matchMedia("(max-width: 700px)").addEventListener("change", closeDrawer);
  closeDrawer();
  $("detail-head").onclick = e => { if (e.target.closest("#clear-scope")) clearScope(); };
  $("search").oninput = e => {
    clearTimeout(state.searchTimer); state.q = e.target.value.trim(); state.page = 1; state.selected.clear();
    // Invalidate an older result immediately, before the debounced request begins.
    state.loadVersion++;
    state.searchTimer = setTimeout(() => { if (state.scope && ["memories", "raw"].includes(state.tab)) loadTab(); }, 300);
  };
  $("filters").onchange = e => {
    const control = e.target.closest("[data-filter]"); if (!control) return;
    state.filters[control.dataset.filter] = control.value; state.page = 1; state.selected.clear(); loadTab();
  };
  $("filters").onclick = e => {
    if (e.target.closest("#clear-filters")) { state.q = ""; state.filters = {}; state.page = 1; state.selected.clear(); loadTab(); }
    if (e.target.closest("#toggle-selection")) { state.selecting = !state.selecting; state.selected.clear(); loadTab(); }
  };
  $("selection-bar").onchange = e => {
    if (e.target.id !== "select-page") return;
    state.selected = new Set(e.target.checked ? state.items.map(row => row.id) : []);
    $("content").querySelectorAll("[data-select]").forEach(input => { input.checked = state.selected.has(Number(input.dataset.select)); }); renderSelection();
  };
  $("selection-bar").onclick = e => { if (e.target.closest("#delete-selection")) removeRecords(state.tab, [...state.selected]); };
  $("prev").onclick = () => { if (state.page > 1) { state.page--; state.selected.clear(); loadTab(); } };
  $("next").onclick = () => { state.page++; state.selected.clear(); loadTab(); };
  $("refresh").onclick = async () => { if (!state.busy && await canLeave()) { clearTimeout(state.searchTimer); await refreshOverview(); await loadTab(); } };
  $("content").onclick = async e => {
    const scope = { ...state.scope };
    const retry = e.target.closest("[data-retry]"); if (retry) { RETRY_LOADERS[retry.dataset.retry]?.(); return; }
    const sources = e.target.closest("[data-sources]"); if (sources) { openSources(Number(sources.dataset.sources), scope); return; }
    const event = e.target.closest("[data-open-event]"); if (event) { openRawEvent(Number(event.dataset.openEvent), scope); return; }
    const edit = e.target.closest("[data-edit-memory]");
    if (edit) { const row = state.items.find(row => row.id === Number(edit.dataset.editMemory)); if (row) openEditor(row, scope, async () => { await refreshOverview(); await loadTab(); }); return; }
    const copy = e.target.closest("[data-copy]");
    if (copy) { try { await navigator.clipboard.writeText(state.items.find(row => row.id === Number(copy.dataset.copy))?.content || ""); toast("已复制"); } catch { toast("无法使用剪贴板，请选择正文复制", "err"); } return; }
    const memory = e.target.closest("[data-del-memory]"), raw = e.target.closest("[data-del-raw]");
    if (memory || raw) { removeRecords(memory ? "memories" : "raw", [Number(memory?.dataset.delMemory || raw.dataset.delRaw)]); return; }
    const tag = e.target.closest("[data-tag]"); if (tag) { state.q = tag.dataset.tag; state.page = 1; state.selected.clear(); loadTab(); return; }
    if (e.target.closest("#reset-scope-cfg")) { resetScopeConfig(); return; }
    if (e.target.closest("#refresh-processing")) { loadProcessingStatus(true); return; }
    const work = e.target.closest("[data-retry-work]");
    if (work && !work.disabled) {
      work.disabled = true;
      const result = await safe("重试任务", () => bridge.apiPost("processing/retry", { id: Number(work.dataset.retryWork), scope_type: work.dataset.scopeType, scope_key: work.dataset.scopeKey }));
      if (result !== null) toast("已加入重试队列");
      if (work.isConnected) { work.disabled = false; loadProcessingStatus(true); }
    }
  };
  $("content").onchange = async e => {
    const selected = e.target.closest("[data-select]");
    if (selected) { const id = Number(selected.dataset.select); selected.checked ? state.selected.add(id) : state.selected.delete(id); renderSelection(); return; }
    const input = e.target.closest("[data-consent]"); if (!input || input.disabled) return;
    const split = input.dataset.consent.indexOf("|");
    input.disabled = true;
    const result = await safe("更新授权", () => bridge.apiPost("consents/toggle", { platform: input.dataset.consent.slice(0, split), sender_id: input.dataset.consent.slice(split + 1), enabled: input.checked }));
    if (result === null) input.checked = !input.checked;
    else { toast("授权已更新"); input.closest(".consent-row").querySelector(".consent-state").textContent = input.checked ? "已授权" : "未授权"; }
    input.disabled = false;
  };
  window.addEventListener("beforeunload", e => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopProcessing(); else if (state.tab === "processing") loadProcessingStatus(); });
  document.addEventListener("memoir:changed", async () => { await refreshOverview(); if (["memories", "raw"].includes(state.tab)) await loadTab(); });
}

RETRY_LOADERS.settings = loadScopeConfigTab;
RETRY_LOADERS.consents = loadConsentPage;
RETRY_LOADERS.overview = refreshOverview;
async function main() {
  const updateTheme = initTheme(); bindEvents(); refreshIcons();
  const context = await bridge.ready(); updateTheme(context); bridge.onContext(updateTheme);
  if (await refreshOverview()) await loadTab();
  else { $("content").innerHTML = emptyState("unplug", "无法加载会话，可点击侧栏刷新重试。"); }
}
main().catch(error => { $("content").innerHTML = `<div class="error-panel"><h3>无法加载记忆管理页</h3><p>${esc(error.message)}</p><button class="btn" onclick="location.reload()">重新加载</button></div>`; });
