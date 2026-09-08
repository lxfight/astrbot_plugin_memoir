import { RETRY_LOADERS, state } from "./state.js";
import { esc, fmtTime, dayLabel, longText } from "./utils.js";
import { loadRecords } from "./list.js";

const statusNames = { pending: "解析排队", running: "正在解析", complete: "已完成", partial: "部分解析", unsupported: "暂不支持", failed: "处理失败", unextracted: "待巩固" };
export function loadRaw() {
  return loadRecords(items => {
    let day = "";
    return `<div class="chat">${items.map(row => {
      const label = dayLabel(row.created_at);
      const divider = label === day ? "" : `<div class="day-chip">${esc(label)}</div>`;
      day = label;
      const forwarded = row.source_kind === "forward_root";
      const status = row.processing_status || "complete";
      const text = forwarded ? `<p>合并转发 · ${row.chunk_count || 0} 个引用分块</p><p class="f-hint">保留节点层级与未验证署名，引用不代表转发者本人陈述。</p>` : longText(row.content);
      return `${divider}<article class="raw-event ${forwarded ? "forward-event" : ""}">${state.selecting ? `<label class="record-select"><input type="checkbox" data-select="${row.id}" aria-label="选择消息 ${row.id}" ${state.selected.has(row.id) ? "checked" : ""}></label>` : ""}<div class="event-body"><div class="msg-head"><strong>${esc(row.speaker_name || "对话")}</strong><span>${fmtTime(row.created_at)}</span><span class="status-chip status-${esc(status)}">${esc(statusNames[status] || status)}</span></div><div class="bubble">${text}</div><div class="record-actions"><button class="btn" data-open-event="${row.id}">${forwarded ? "展开转发层级" : "查看完整消息"}</button><button class="btn" data-copy="${row.id}">${forwarded ? "复制概要" : "复制"}</button><button class="btn danger" data-del-raw="${row.id}">删除${forwarded ? "整个转发" : "消息"}</button></div><small>#${row.id}${row.speaker_id ? ` · 发送者 ${esc(row.speaker_id)}` : ""}</small></div></article>`;
    }).join("")}</div>`;
  });
}
RETRY_LOADERS.raw = loadRaw;
