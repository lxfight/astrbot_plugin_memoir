import { RETRY_LOADERS, TYPE_NAMES, state } from "./state.js";
import { esc, fmtTime, longText } from "./utils.js";
import { loadRecords } from "./list.js";

export function loadMemories() {
  return loadRecords(items => `<div class="list-pad">${items.map(m => `<article class="mem t-${esc(m.memory_type)}">
    ${state.selecting ? `<label class="record-select"><input type="checkbox" data-select="${m.id}" aria-label="选择记忆 ${m.id}" ${state.selected.has(m.id) ? "checked" : ""}></label>` : ""}
    <div class="mem-body"><div class="mem-top"><span class="tag-chip badge">${esc(TYPE_NAMES[m.memory_type] || m.memory_type)}</span>${m.source_type === "forwarded" ? '<span class="subject-chip">转发引用 · 非个人画像</span>' : ""}${m.manually_edited_at ? '<span class="subject-chip">人工修订</span>' : ""}${m.subject ? `<span class="subject-chip">${esc(m.subject)}</span>` : ""}</div>
    <div class="mem-content">${longText(m.content)}</div>
    <div class="mem-tags">${String(m.tags || "").split(/[,，]/).filter(Boolean).slice(0, 6).map(tag => `<button class="mem-tag" data-tag="${esc(tag)}">#${esc(tag)}</button>`).join("")}</div>
    <div class="record-actions"><button class="btn" data-sources="${m.id}">查看来源</button><button class="btn" data-edit-memory="${m.id}">编辑</button><button class="btn" data-copy="${m.id}">复制</button><button class="btn danger" data-del-memory="${m.id}">删除</button></div>
    <details class="record-meta"><summary>重要度 ${m.importance} · ${fmtTime(m.updated_at)}</summary><p>#${m.id} · 记忆强度 ${Number(m.strength || 0).toFixed(2)}${m.manually_edited_at ? ` · 人工修订于 ${fmtTime(m.manually_edited_at)}` : ""}</p></details></div>
  </article>`).join("")}</div>`);
}
RETRY_LOADERS.memories = loadMemories;
