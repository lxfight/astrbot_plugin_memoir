import { $, state } from "./state.js";
import { esc, longText, refreshIcons, toast } from "./utils.js";
import { bridge, safe } from "./api.js";

export function ask(title, message, accept = "确认", danger = false) {
  const dialog = $("confirm-dialog");
  if (dialog.open) return Promise.resolve(false);
  dialog.innerHTML = `<form method="dialog"><h2 class="text-h3 pa-4 pb-0 pl-6">${esc(title)}</h2><p class="dialog-message">${esc(message)}</p><div class="dialog-actions"><button class="btn" value="cancel" autofocus>取消</button><button class="btn ${danger ? "danger" : "primary"}" value="accept">${esc(accept)}</button></div></form>`;
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "accept"), { once: true }));
}

function signature(form) {
  return JSON.stringify([...form.querySelectorAll("input,select,textarea")].map(el => [el.id || el.name, el.type === "checkbox" ? el.checked : el.value]));
}
export function markSaved(form) {
  form.dataset.baseline = signature(form);
  const update = () => {
    const dirty = signature(form) !== form.dataset.baseline;
    form.dataset.dirty = String(dirty);
    const label = form.querySelector(".save-state");
    if (label) label.textContent = dirty ? "有未保存修改" : "已保存";
  };
  form.oninput = update;
  form.onchange = update;
  update();
}
export function isDirty() {
  return [...document.querySelectorAll("[data-baseline]")].some(form => signature(form) !== form.dataset.baseline);
}
export async function canLeave() {
  if (document.querySelector('[data-saving="true"]')) { toast("正在保存，请稍候"); return false; }
  return !isDirty() || await ask("放弃未保存修改？", "切换页面后，尚未保存的修改将丢失。", "放弃修改", true);
}

export function openEditor(row, scope, onSaved) {
  const dialog = $("editor-dialog");
  dialog.innerHTML = `<form id="memory-editor"><h2 class="text-h3 pa-4 pb-0 pl-6">编辑记忆 #${row.id}</h2><p class="dialog-message">${row.source_type === "forwarded" ? "这是一条转发引用。修订后仍保留引用属性和原始来源。" : "修改正文、标签和重要度，原始来源与归属保持不变。"}</p><label>正文<textarea id="edit-content" required maxlength="4000" rows="7">${esc(row.content)}</textarea></label><label>标签（逗号分隔）<input id="edit-tags" maxlength="200" value="${esc(row.tags || "")}"></label><label>重要度<select id="edit-importance">${[1,2,3,4,5].map(n => `<option ${n === row.importance ? "selected" : ""}>${n}</option>`).join("")}</select></label><p id="edit-error" role="alert" class="dialog-message"></p><div class="dialog-actions"><span class="save-state"></span><button type="button" class="btn" id="cancel-edit">取消</button><button class="btn primary" type="submit">保存修改</button></div></form>`;
  const form = $("memory-editor");
  markSaved(form);
  const close = async () => { if (await canLeave()) { delete form.dataset.baseline; dialog.close(); } };
  $("cancel-edit").onclick = close;
  dialog.oncancel = e => { e.preventDefault(); close(); };
  dialog.onclose = () => { dialog.innerHTML = ""; };
  form.onsubmit = async e => {
    e.preventDefault();
    if (form.dataset.saving === "true" || !form.reportValidity()) return;
    const payload = { ...scope, id: row.id, revision: row.edit_revision || 0, content: $("edit-content").value, tags: $("edit-tags").value, importance: Number($("edit-importance").value) };
    form.dataset.saving = "true"; form.inert = true;
    const result = await safe("保存记忆", () => bridge.apiPost("memories/update", payload));
    form.dataset.saving = "false"; form.inert = false;
    if (result === null) { $("edit-error").textContent = $("toast").textContent || "保存失败，请重试"; return; }
    markSaved(form); dialog.close(); toast("人工修订已保存"); await onSaved();
  };
  dialog.showModal();
}

let detailVersion = 0;
export async function openSources(id, scope) {
  const version = ++detailVersion;
  const dialog = $("detail-dialog");
  dialog.innerHTML = `<h2 class="text-h3 pa-4 pb-0 pl-6">记忆来源 #${id}</h2><div id="source-body">加载中…</div><form method="dialog" class="dialog-actions"><button class="btn" autofocus>关闭</button></form>`;
  if (!dialog.open) dialog.showModal();
  const result = await safe("读取来源", () => bridge.apiGet("memories/sources", { ...scope, id }));
  if (version !== detailVersion || !dialog.open) return;
  $("source-body").innerHTML = result ? result.items.map(item => `<article class="source-card"><small>#${item.id} · ${esc(item.speaker_name || "引用原文")}</small>${longText(item.content)}<button class="btn" data-open-event="${item.id}">查看所属消息</button></article>`).join("") + (result.expired ? `<p>${result.expired} 条来源已删除或过期</p>` : "") + (!result.tracked ? "历史记忆未记录来源" : "") : "加载失败，请关闭后重试";
  dialog.onclick = e => { const button = e.target.closest("[data-open-event]"); if (button) openRawEvent(Number(button.dataset.openEvent), scope, () => openSources(id, scope)); };
}

export async function openRawEvent(id, scope, back = null) {
  const version = ++detailVersion;
  const dialog = $("detail-dialog");
  dialog.innerHTML = `<h2 class="text-h3 pa-4 pb-0 pl-6">原始消息 #${id}</h2><div id="source-body">加载中…</div><form method="dialog" class="dialog-actions">${back ? '<button class="btn" type="button" id="source-back">返回记忆来源</button>' : ""}<button class="btn" autofocus>关闭</button></form>`;
  if (!dialog.open) dialog.showModal();
  if (back) $("source-back").onclick = back;
  const result = await safe("读取完整消息", () => bridge.apiGet("raw/event", { ...scope, id }));
  if (version !== detailVersion || !dialog.open) return;
  if (!result) { $("source-body").textContent = "来源已删除，或当前无法读取。"; return; }
  dialog.querySelector("h2").textContent = `原始消息 #${result.root.id}${id !== result.root.id ? ` · 定位分块 #${id}` : ""}`;
  const tree = { children: new Map(), rows: [] };
  for (const row of result.items) {
    let node = tree;
    for (const part of (row.node_path || "media").split(".")) {
      if (!node.children.has(part)) node.children.set(part, { children: new Map(), rows: [] });
      node = node.children.get(part);
    }
    node.rows.push(row);
  }
  function renderTree(node, path = []) {
    return node.rows.map(row => {
      let meta = {}; try { meta = JSON.parse(row.source_meta || "{}"); } catch { /* Legacy metadata. */ }
      return `<article class="source-card ${row.id === result.selected_id ? "source-selected" : ""}" id="source-${row.id}"><small>#${row.id} · ${esc(meta.name || "未知署名")} (${esc(meta.id || "未知 ID")}) · 身份未验证 · 原时间 ${esc(meta.time || "未知")}</small>${longText(row.content)}<button class="btn" data-copy-source="${row.id}">复制</button><button class="btn danger" data-delete-chunk="${row.id}">删除此分块</button></article>`;
    }).join("") + [...node.children].map(([key, child]) => `<details class="forward-node" open><summary>节点 ${esc([...path, key].join("."))}</summary>${renderTree(child, [...path, key])}</details>`).join("");
  }
  const records = [result.root, ...result.items];
  $("source-body").innerHTML = `<p id="source-feedback" role="status"></p><article class="source-card"><strong>原始消息 #${result.root.id}</strong>${longText(result.root.content)}<button class="btn" data-copy-source="all">复制完整消息</button></article>${renderTree(tree)}`;
  dialog.onclick = async e => {
    const copy = e.target.closest("[data-copy-source]");
    if (copy) {
      const text = copy.dataset.copySource === "all" ? records.map(row => row.content).join("\n\n") : records.find(row => row.id === Number(copy.dataset.copySource))?.content || "";
      try { await navigator.clipboard.writeText(text); $("source-feedback").textContent = "已复制"; } catch { $("source-feedback").textContent = "无法使用剪贴板，请选择正文复制"; }
    }
    const button = e.target.closest("[data-delete-chunk]");
    if (!button || state.busy) return;
    state.busy = true;
    try {
      if (!await ask("删除这个引用分块？", "只删除选中分块，并取消所属事件的后续解析任务。已经提炼的记忆不会自动删除。", "删除分块", true)) return;
      const result = await safe("删除分块", () => bridge.apiPost("records/delete", { ...scope, kind: "raw", ids: [Number(button.dataset.deleteChunk)] }));
      if (result !== null) { await openRawEvent(records[0].id, scope, back); if ($("source-feedback")) $("source-feedback").textContent = "分块已删除"; document.dispatchEvent(new Event("memoir:changed")); }
      else if ($("source-feedback")) $("source-feedback").textContent = $("toast").textContent;
    } finally { state.busy = false; }
  };
  refreshIcons();
  dialog.querySelector(".source-selected")?.scrollIntoView({ block: "nearest" });
}
