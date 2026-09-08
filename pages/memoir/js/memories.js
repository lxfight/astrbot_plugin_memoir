import { $, beginLoad, RETRY_LOADERS, TYPE_ICONS, TYPE_NAMES, state } from "./state.js";
import { esc, emptyState, errorPanel, fmtTime, refreshIcons, renderPager, skeleton, stagger } from "./utils.js";
import { bridge, safe } from "./api.js";

function parseTags(m) {
  const raw = String(m.tags || "").trim();
  if (!raw) return [];
  return raw
    .split(/[,，]/)
    .map((t) => t.trim())
    .filter(Boolean)
    .slice(0, 6);
}

function importanceMeter(v) {
  const n = Math.max(0, Math.min(5, Number(v) || 0));
  return `<span class="meter"><span class="dots">${Array.from({ length: 5 }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("")}</span></span>`;
}

function memoryItem(m, i) {
  const type = TYPE_NAMES[m.memory_type] || m.memory_type;
  const icon = TYPE_ICONS[m.memory_type] || "message-circle";
  const tags = parseTags(m);
  const strength = Math.max(0, Math.min(1, Number(m.strength) || 0));
  return `<div class="mem t-${esc(m.memory_type)}"${stagger(i)}>
    <div class="mem-body">
      <div class="mem-top">
        <span class="tag-chip badge t-${esc(m.memory_type)}"><i data-lucide="${icon}"></i>${esc(type)}</span>
        ${m.source_type === "forwarded" ? `<span class="subject-chip">转发引用 · 非个人画像</span>` : ""}
        ${m.subject ? `<span class="subject-chip"><i data-lucide="user-round"></i>${esc(m.subject)}</span>` : ""}
      </div>
      <div class="mem-content">${esc(m.content)}</div>
      ${tags.length ? `<div class="mem-tags">${tags.map((t) => `<span class="mem-tag" data-tag="${esc(t)}">#${esc(t)}</span>`).join("")}</div>` : ""}
      <details class="memory-sources" data-memory-source="${m.id}" data-scope-type="${esc(state.scope.scope_type)}" data-scope-key="${esc(state.scope.scope_key)}"><summary>查看来源</summary><div class="source-content">展开后加载来源</div></details>
      <div class="mem-foot">
        <span class="strength-bar"><span class="bar"><i style="width:${Math.round(strength * 100)}%"></i></span>强度 ${(strength).toFixed(2)}</span>
        ${importanceMeter(m.importance)}<span>重要度 ${m.importance ?? "-"}</span>
        <span>${fmtTime(m.updated_at)}</span>
        <span>#${m.id}</span>
      </div>
    </div>
    <button class="del" data-del-memory="${m.id}" title="删除"><i data-lucide="trash-2"></i></button>
  </div>`;
}

export async function loadMemories() {
  const request = beginLoad();
  $("content").innerHTML = skeleton();
  const params = request.scope;
  const res = await safe(
    "加载记忆",
    () =>
      state.q
        ? bridge.apiGet("memories", { ...params, q: state.q })
        : bridge.apiGet("memories", { ...params, page: state.page, page_size: state.pageSize }),
    { silent: true },
  );
  if (!request.current()) return;
  if (!res) {
    $("content").innerHTML = errorPanel("加载记忆", "请求失败，请检查后端状态后重试", "memories");
    refreshIcons();
    return;
  }
  let html;
  if (state.q) {
    html = res.items.length
      ? `<div class="list-pad">${res.items.map(memoryItem).join("")}</div>`
      : emptyState("search-x", `没有找到与「${esc(state.q)}」相关的记忆`);
    renderPager(0, state.pageSize);
  } else {
    html = res.items.length
      ? `<div class="list-pad">${res.items.map(memoryItem).join("")}</div>`
      : emptyState("inbox", "当前会话还没有结构化记忆");
    renderPager(res.total, res.page_size || state.pageSize);
  }
  $("content").innerHTML = html;
  refreshIcons();
}

RETRY_LOADERS.memories = () => loadMemories();
