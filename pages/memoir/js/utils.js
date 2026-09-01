import { $, state } from "./state.js";

export const REDUCED_MOTION =
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

export const refreshIcons = () => window.lucide && window.lucide.createIcons();

let toastTimer = null;
export function toast(msg, kind = "ok") {
  const el = $("toast");
  el.className = `toast show ${kind}`;
  el.innerHTML = `<i data-lucide="${kind === "err" ? "circle-alert" : "circle-check"}"></i>${esc(msg)}`;
  refreshIcons();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), kind === "err" ? 3600 : 2200);
}

export const errorPanel = (label, msg, retryKey) => `
  <div class="error-panel">
    <div class="empty-icon"><i data-lucide="triangle-alert"></i></div>
    <h3>${esc(label)}失败</h3>
    <p class="err-msg">${esc(msg)}</p>
    <button class="btn" data-retry="${esc(retryKey)}"><i data-lucide="rotate-cw"></i>重试</button>
  </div>`;

export function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function timeAgo(ts) {
  if (!ts) return "-";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return fmtTime(ts).slice(0, 10);
}

export function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  const m = d.getMonth() + 1;
  if (d.toDateString() === now.toDateString()) return "今天";
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return "昨天";
  return sameYear ? `${m}月${d.getDate()}日` : `${d.getFullYear()}年${m}月${d.getDate()}日`;
}

export const emptyState = (icon, msg, withTitle) =>
  `<div class="empty"><div class="empty-icon"><i data-lucide="${icon}"></i></div>${withTitle ? `<h3>${withTitle}</h3>` : ""}<p>${msg}</p></div>`;

export const skeleton = (n = 5) =>
  `<div class="skeleton">${Array.from({ length: n }, () => '<div class="item-row"></div>').join("")}</div>`;

export const stagger = (i, base = 0, step = 30, cap = 300) =>
  REDUCED_MOTION ? "" : ` style="animation-delay:${Math.min(base + i * step, cap)}ms"`;

export function renderPager(total, pageSize) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) {
    $("pager").style.display = "none";
    return;
  }
  $("pager").style.display = "flex";
  $("page-info").textContent = `${state.page} / ${pages} · 共 ${total} 条`;
  $("prev").disabled = state.page <= 1;
  $("next").disabled = state.page >= pages;
}
