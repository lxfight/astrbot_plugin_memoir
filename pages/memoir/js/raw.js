import { $, RETRY_LOADERS, state } from "./state.js";
import { esc, emptyState, errorPanel, fmtTime, dayLabel, refreshIcons, renderPager, skeleton, stagger } from "./utils.js";
import { bridge, safe } from "./api.js";

/* ---------- raw turns: chat bubbles ---------- */
// Parse one raw turn into bubbles: private format "用户: … / 助手: …", bridged copy "用户: …"
function parseTurn(content) {
  const s = String(content);
  let m = s.match(/^用户[:：]\s*([\s\S]*?)\s*\/\s*助手[:：]\s*([\s\S]+)$/);
  if (m) {
    return [
      { who: "me", text: m[1] },
      { who: "bot", text: m[2] },
    ];
  }
  const u = s.match(/^用户[:：]\s*([\s\S]+)$/);
  if (u) return [{ who: "me", text: u[1] }];
  return [{ who: "other", text: s }];
}

function speakerHue(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

function bubbleText(text) {
  // Highlight placeholders like [图片] [语音x2], escape the rest
  return esc(text).replace(
    /\[(图片|语音|文件|视频|表情)(?:x(\d+))?\]/g,
    (_, kind, n) => `<span class="placeholder-chip">[${kind}${n ? `x${n}` : ""}]</span>`,
  );
}

function rawTurnHtml(r, prevDay, i) {
  const day = new Date((r.created_at || 0) * 1000).toDateString();
  const chip = day !== prevDay ? `<div class="day-chip"${stagger(i, 20)}>${esc(dayLabel(r.created_at || 0))}</div>` : "";
  const pending = r.extracted
    ? ""
    : `<span class="pending-pill">待巩固</span>`;
  const speaker = r.speaker_name
    ? `<span class="speaker" style="color:hsl(${speakerHue(r.speaker_name)} 45% 42%)">${esc(r.speaker_name)}</span> ·`
    : "";
  const head = `<div class="msg-head">${speaker}<span>${fmtTime(r.created_at)}</span><span>#${r.id}</span>${pending}<button class="del" data-del-raw="${r.id}" title="删除这轮"><i data-lucide="trash-2"></i></button></div>`;
  const parts = parseTurn(r.content);
  const rows = parts
    .map((p, k) => {
      let avatar;
      if (p.who === "bot") {
        avatar = `<span class="avatar"><i data-lucide="bot"></i></span>`;
      } else if (p.who === "me") {
        avatar = `<span class="avatar"><i data-lucide="user-round"></i></span>`;
      } else {
        const name = r.speaker_name || "?";
        avatar = `<span class="avatar" style="background:hsl(${speakerHue(name)} 42% 48%)">${esc(name.slice(0, 1))}</span>`;
      }
      // One row per role; meta info (time/delete) only attached to first message
      return `<div class="msg ${p.who}"${stagger(i, 20, 30, 260)}>${avatar}<div class="msg-main">${k === 0 ? head : ""}<div class="bubble">${bubbleText(p.text)}</div></div></div>`;
    })
    .join("");
  return { chip, html: rows, day };
}

function renderRawTurns(items) {
  let html = "";
  let prevDay = "";
  let i = 0;
  for (const r of items) {
    const { chip, html: msgHtml, day } = rawTurnHtml(r, prevDay, i);
    html += chip + msgHtml;
    prevDay = day;
    i++;
  }
  return `<div class="chat">${html}</div>`;
}

export async function loadRaw() {
  $("content").innerHTML = skeleton();
  const res = await safe(
    "加载原文",
    () =>
      bridge.apiGet("raw", {
        scope_type: state.scope.scope_type,
        scope_key: state.scope.scope_key,
        page: state.page,
        page_size: state.pageSize,
      }),
    { silent: true },
  );
  if (!res) {
    $("content").innerHTML = errorPanel("加载原文对话", "请求失败，请检查后端状态后重试", "raw");
    refreshIcons();
    return;
  }
  $("content").innerHTML = res.items.length
    ? renderRawTurns(res.items)
    : emptyState("messages-square", "当前会话还没有原文对话");
  renderPager(res.total, res.page_size || state.pageSize);
  refreshIcons();
}

RETRY_LOADERS.raw = () => loadRaw();
