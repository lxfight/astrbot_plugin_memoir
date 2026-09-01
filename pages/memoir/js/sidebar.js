import { $, state } from "./state.js";
import { esc, refreshIcons, timeAgo, emptyState, stagger, REDUCED_MOTION } from "./utils.js";

/* ---------- stats & sidebar ---------- */
function countUp(el, target, dur = 550) {
  if (REDUCED_MOTION || target === 0) {
    el.textContent = target;
    return;
  }
  const start = performance.now();
  const tick = (t) => {
    const k = Math.min(1, (t - start) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(target * eased);
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

export function renderOverview(totals) {
  const items = [
    ["c-scope", "layers", "会话", totals.scopes],
    ["c-memory", "brain", "结构化记忆", totals.memories],
    ["c-raw", "messages-square", "原文轮次", totals.raw_turns],
    ["c-pending", "clock-3", "待巩固", totals.pending],
  ];
  $("cards").innerHTML = items
    .map(
      ([cls, icon, label, num], i) =>
        `<div class="st ${cls}"${stagger(i, 0, 50)}><span class="chip"><i data-lucide="${icon}"></i></span><div><div class="num" data-count="${num}">0</div><div class="label">${label}</div></div></div>`,
    )
    .join("");
  $("cards").querySelectorAll(".num").forEach((el) => countUp(el, Number(el.dataset.count) || 0));
  refreshIcons();
}

function scopeName(s) {
  return `${s.scope_type === "private" ? "私聊" : "群聊"} · ${s.scope_key.split(":").slice(1).join(":")}`;
}

export function renderSidebar() {
  const kw = state.scopeFilter.trim().toLowerCase();
  const filtered = state.scopes.filter(
    (s) => !kw || s.scope_key.toLowerCase().includes(kw) || s.scope_type.includes(kw),
  );
  if (!filtered.length) {
    $("scope-list").innerHTML = emptyState(
      state.scopes.length ? "search-x" : "inbox",
      state.scopes.length ? "没有匹配的会话" : "与机器人对话后，会话会出现在这里",
    );
    refreshIcons();
    return;
  }
  const groups = [
    ["private", "user-round", "私聊"],
    ["group", "users-round", "群聊"],
  ];
  let html = "";
  let i = 0;
  for (const [type, icon, label] of groups) {
    const list = filtered.filter((s) => s.scope_type === type);
    if (!list.length) continue;
    html += `<div class="group-title"><i data-lucide="${icon}"></i>${label}<span class="count">${list.length}</span></div>`;
    for (const s of list) {
      const active = state.scope && state.scope.scope_key === s.scope_key;
      html += `
        <button class="scope-item ${active ? "active" : ""}" data-scope="${esc(s.scope_type)}|${esc(s.scope_key)}"${stagger(i, 40)}>
          <span class="chip"><i data-lucide="${icon}"></i></span>
          <span class="info">
            <span class="name"><span>${esc(scopeName(s))}</span>${s.memory_count === 0 ? '<span class="disabled-tag">空</span>' : ""}</span>
            <span class="meta">${s.memory_count} 记忆 · ${timeAgo(s.last_activity_at)}${s.pending_count ? ` · <span class="pending">${s.pending_count} 待巩固</span>` : ""}</span>
          </span>
          ${s.pending_count ? '<span class="badge-dot"></span>' : ""}
        </button>`;
      i++;
    }
  }
  $("scope-list").innerHTML = html;
  refreshIcons();
}

/* ---------- detail ---------- */
export function renderDetailHead() {
  const s = state.scopes.find(
    (x) => state.scope && x.scope_key === state.scope.scope_key,
  );
  const hasOverride = Object.keys(state.scopeOverride).length > 0;
  const disabled = state.scopeOverride.enabled === false;
  $("detail-head").innerHTML = `
    <span class="chip"><i data-lucide="${s?.scope_type === "group" ? "users-round" : "user-round"}"></i></span>
    <div style="min-width:0">
      <div class="name">${esc(state.scope ? scopeName(state.scope) : "")}</div>
      <div class="sub">${esc(state.scope?.scope_key || "")}</div>
    </div>
    ${
      s
        ? `<div class="head-stats">
            <span class="hstat"><i data-lucide="brain"></i>${s.memory_count} 记忆</span>
            <span class="hstat"><i data-lucide="messages-square"></i>${s.raw_count} 原文</span>
            ${s.pending_count ? `<span class="hstat h-pending"><i data-lucide="clock-3"></i>${s.pending_count} 待巩固</span>` : ""}
          </div>`
        : '<div style="flex:1"></div>'
    }
    ${disabled ? '<span class="status-badge off"><i data-lucide="circle-pause"></i>已禁用</span>' : ""}
    ${hasOverride && !disabled ? '<span class="status-badge custom"><i data-lucide="sliders-horizontal"></i>自定义</span>' : ""}
    <button class="btn danger" id="clear-scope"><i data-lucide="eraser"></i>清空会话</button>`;
  refreshIcons();
}
