import { state } from "./state.js";
import { esc, fmtTime, dayLabel, longText } from "./utils.js";

const statusNames = { skipped: "策略跳过", paused: "额度暂停", pending: "解析排队", running: "正在解析", complete: "已完成", partial: "部分解析", unsupported: "暂不支持", failed: "处理失败", unextracted: "待巩固" };
export function renderRawEvent(row, previousDay) {
  const label = dayLabel(row.created_at);
  const divider = label === previousDay ? "" : `<div class="day-chip">${esc(label)}</div>`;
  const forwarded = row.source_kind === "forward_root";
  const status = row.processing_status || (row.extracted === -1 ? "failed" : row.extracted ? "complete" : "unextracted");
  const sender = row.speaker_name || "用户";
  const content = String(row.content || "");
  // Only native turns use the stored role separators; quoted text keeps its identity.
  const native = !row.source_kind || row.source_kind === "native";
  const pair = native && content.match(/^用户[:：]\s*([\s\S]*?)\s*\/\s*助手[:：]\s*([\s\S]+)$/);
  const user = native && content.match(/^用户[:：]\s*([\s\S]+)$/);
  const assistant = native && content.match(/^助手[:：]\s*([\s\S]+)$/);
  const parts = pair ? [{ role: "me", text: pair[1] }, { role: "bot", text: pair[2] }]
    : [{ role: user ? "me" : assistant ? "bot" : "other", text: user ? user[1] : assistant ? assistant[1] : content }];
  const actions = `<details class="message-menu" name="raw-actions"><summary aria-label="消息 ${row.id} 的操作" title="消息操作"><i data-lucide="ellipsis" aria-hidden="true"></i></summary><div class="message-menu-panel"><small>原始记录 #${row.id}${row.speaker_id ? ` · ${esc(row.speaker_id)}` : ""}</small><button class="btn" data-open-event="${row.id}"><i data-lucide="${forwarded ? "git-branch" : "expand"}" aria-hidden="true"></i>${forwarded ? "展开转发层级" : "查看完整消息"}</button><button class="btn" data-copy="${row.id}"><i data-lucide="copy" aria-hidden="true"></i>${forwarded ? "复制概要" : "复制原始对话"}</button><button class="btn danger" data-del-raw="${row.id}"><i data-lucide="trash-2" aria-hidden="true"></i>删除${forwarded ? "整个转发" : pair ? "整轮对话" : "消息"}</button></div></details>`;
  let hue = 205;
  for (const ch of sender) hue = (hue * 31 + ch.codePointAt(0)) % 360;
  const messages = (forwarded ? [{ role: "other" }] : parts).map((part, index) => {
    const bot = part.role === "bot";
    const name = bot ? "助手" : native || forwarded ? sender : "引用内容";
    const avatar = bot ? '<img src="./logo.png" alt="">' : row.speaker_name ? esc(Array.from(sender)[0]) : `<i data-lucide="${forwarded ? "forward" : native ? "user-round" : "quote"}"></i>`;
    const body = forwarded
      ? `<button class="forward-preview" data-open-event="${row.id}"><span class="forward-label"><i data-lucide="forward" aria-hidden="true"></i>转发的消息</span><strong>合并转发</strong><span class="forward-count">${Number(row.chunk_count) || 0} 个引用分块</span><span class="forward-open">查看聊天记录<i data-lucide="chevron-right" aria-hidden="true"></i></span></button><p class="forward-note">引用署名未经验证，不代表转发者本人陈述。</p>`
      : longText(part.text);
    return `<div class="msg ${part.role}" style="--speaker-hue:${hue}"><span class="avatar" aria-hidden="true">${avatar}</span><div class="msg-main"><div class="bubble"><div class="msg-head"><strong>${esc(name)}</strong>${!native && !forwarded ? '<span>署名未验证</span>' : ""}</div>${body}<div class="bubble-meta">${index === 0 ? `<span class="message-status status-${esc(status)}">${esc(statusNames[status] || status)}</span>` : ""}<time title="${fmtTime(row.created_at)}">${fmtTime(row.created_at).slice(-5)}</time>${index === 0 ? actions : ""}</div></div></div></div>`;
  }).join("");
  return `${divider}<article class="raw-event ${forwarded ? "forward-event" : ""}">${state.selecting ? `<label class="record-select"><input type="checkbox" data-select="${row.id}" aria-label="选择消息 ${row.id}" ${state.selected.has(row.id) ? "checked" : ""}></label>` : ""}<div class="event-messages">${messages}</div></article>`;
}
