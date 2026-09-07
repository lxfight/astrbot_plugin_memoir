import { $, RETRY_LOADERS, state } from "./state.js";
import { esc, refreshIcons, toast } from "./utils.js";
import { bridge, safe } from "./api.js";
import { initParticles } from "./particles.js";
import { initTheme } from "./theme.js";
import { renderOverview, renderSidebar, renderDetailHead } from "./sidebar.js";
import { loadMemories } from "./memories.js";
import { loadRaw } from "./raw.js";
import { loadScopeConfigTab, saveScopeConfig, resetScopeConfig, saveGlobalConfig } from "./settings.js";

// Toast rejections raised by page modules only; content scripts from
// browser extensions also bubble into this handler and stay console-only
const isOwnRejection = (reason) => {
  const stack = reason instanceof Error ? reason.stack : "";
  return typeof stack === "string" && stack.includes("/api/plugin/page/");
};

window.addEventListener("unhandledrejection", (e) => {
  console.error("[Memoir] unhandled:", e.reason);
  if (isOwnRejection(e.reason)) toast(`出错了：${e.reason?.message || e.reason}`, "err");
});
window.addEventListener("error", (e) => {
  console.error("[Memoir] runtime error:", e.error || e.message);
});

/* ---------- tabs & events ---------- */
async function loadTab() {
  $("search-wrap").style.display = state.tab === "memories" ? "flex" : "none";
  if (state.tab === "settings") $("pager").style.display = "none";
  if (!state.scope) {
    $("pager").style.display = "none";
    $("detail-head").innerHTML = "";
    $("content").innerHTML = renderNoScope();
    refreshIcons();
    return;
  }
  renderDetailHead();
  if (state.tab === "memories") await loadMemories();
  else if (state.tab === "raw") await loadRaw();
  else await loadScopeConfigTab();
}

function renderNoScope() {
  return `
    <div class="empty">
      <div class="empty-icon"><i data-lucide="inbox"></i></div>
      <h3>还没有任何记忆</h3>
      <p>与机器人对话后，会话会自动出现在这里</p>
      <div class="empty-steps">
        <span class="step"><i data-lucide="message-circle"></i>正常聊天</span>
        <i data-lucide="arrow-right"></i>
        <span class="step"><i data-lucide="sparkles"></i>后台自动巩固</span>
        <i data-lucide="arrow-right"></i>
        <span class="step"><i data-lucide="database"></i>在此管理</span>
      </div>
    </div>`;
}

async function clearScope() {
  if (!state.scope) return;
  if (!confirm(`确定清空会话 ${state.scope.scope_key} 的全部记忆与原文？此操作不可恢复。`)) return;
  const res = await safe("清空会话", () => bridge.apiPost("scope/clear", state.scope));
  if (res === null) return;
  toast(`已清空 ${res.deleted ?? 0} 条记录`);
  await refreshAll();
}

async function refreshAll(spin = false) {
  $("refresh").classList.toggle("spinning", spin);
  const overview = await safe("刷新概览", () => bridge.apiGet("overview"), { silent: true });
  $("refresh").classList.remove("spinning");
  if (!overview) {
    toast("刷新失败：无法连接记忆服务", "err");
    return false;
  }
  renderOverview(overview.totals);
  state.scopes = overview.scopes || [];
  if (state.scope) {
    const cur = state.scopes.find((s) => s.scope_key === state.scope.scope_key);
    if (!cur) state.scope = state.scopes[0] || null;
  } else {
    state.scope = state.scopes[0] || null;
  }
  renderSidebar();
  await loadTab();
  return true;
}

function bindEvents() {
  $("detail-head").addEventListener("click", (e) => {
    if (e.target.closest("#clear-scope")) clearScope();
  });

  $("tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-pressed", String(t === tab));
    });
    state.tab = tab.dataset.tab;
    state.page = 1;
    loadTab();
  });

  $("scope-list").addEventListener("click", (e) => {
    const item = e.target.closest("[data-scope]");
    if (!item) return;
    const [scope_type, scope_key] = item.dataset.scope.split("|");
    state.scope = { scope_type, scope_key };
    state.page = 1;
    renderSidebar();
    loadTab();
  });

  $("scope-filter").addEventListener("input", (e) => {
    state.scopeFilter = e.target.value;
    renderSidebar();
  });

  $("search").addEventListener("input", (e) => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.q = e.target.value.trim();
      state.page = 1;
      if (state.scope && state.tab === "memories") loadMemories();
    }, 300);
  });

  $("prev").addEventListener("click", () => {
    if (state.page > 1) { state.page--; loadTab(); }
  });
  $("next").addEventListener("click", () => {
    state.page++; loadTab();
  });

  $("content").addEventListener("click", async (e) => {
    if (e.target.closest("#save-scope-cfg")) { saveScopeConfig(); return; }
    if (e.target.closest("#reset-scope-cfg")) { resetScopeConfig(); return; }
    if (e.target.closest("#save-global-cfg")) { saveGlobalConfig(); return; }
    const retry = e.target.closest("[data-retry]");
    if (retry) {
      const fn = RETRY_LOADERS[retry.dataset.retry];
      if (fn) fn();
      return;
    }
    const tagChip = e.target.closest("[data-tag]");
    if (tagChip && state.tab === "memories") {
      $("search").value = tagChip.dataset.tag;
      state.q = tagChip.dataset.tag;
      state.page = 1;
      loadMemories();
      return;
    }
    const delMemory = e.target.closest("[data-del-memory]");
    const delRaw = e.target.closest("[data-del-raw]");
    if (delMemory && confirm("确定删除这条记忆？")) {
      const ok = await safe("删除记忆", () =>
        bridge.apiPost("memories/delete", { id: Number(delMemory.dataset.delMemory), ...state.scope }),
      );
      if (ok !== null) {
        toast("已删除");
        loadTab();
      }
    } else if (delRaw && confirm("确定删除这轮对话？")) {
      const ok = await safe("删除原文", () =>
        bridge.apiPost("raw/delete", { id: Number(delRaw.dataset.delRaw), ...state.scope }),
      );
      if (ok !== null) {
        toast("已删除");
        loadTab();
      }
    }
  });

  $("content").addEventListener("change", async (e) => {
    const input = e.target.closest("[data-consent]");
    if (!input) return;
    const [platform, sender_id] = input.dataset.consent.split("|");
    const ok = await safe("更新授权", () =>
      bridge.apiPost("consents/toggle", { platform, sender_id, enabled: input.checked }),
    );
    if (ok !== null) toast(input.checked ? "已开启授权" : "已关闭授权");
    else input.checked = !input.checked;
  });

  $("refresh").addEventListener("click", () => refreshAll(true));
}

async function main() {
  const updateThemeContext = initTheme();
  initParticles();
  bindEvents();
  refreshIcons();
  const ctx = await bridge.ready();
  updateThemeContext(ctx);
  bridge.onContext(updateThemeContext);
  await refreshAll();
}

RETRY_LOADERS.overview = () => refreshAll(true);

main().catch((err) => {
  const msg = err?.message || String(err);
  $("content").innerHTML = `
    <div class="error-panel">
      <div class="empty-icon"><i data-lucide="unplug"></i></div>
      <h3>无法加载记忆管理页</h3>
      <p class="err-msg">${esc(msg)}</p>
      <button class="btn" onclick="location.reload()"><i data-lucide="rotate-cw"></i>重新加载</button>
    </div>`;
  refreshIcons();
});
