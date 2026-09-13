import { esc } from "./utils.js";
export const MEDIA_FIELDS = [
  {
    "key": "image_mode",
    "label": "图片处理方式",
    "type": "select",
    "options": [
      "auto",
      "manual",
      "off"
    ]
  },
  {
    "key": "audio_mode",
    "label": "音频处理方式",
    "type": "select",
    "options": [
      "auto",
      "manual",
      "off"
    ]
  },
  {
    "key": "image_group_trigger",
    "label": "群聊图片范围",
    "type": "select",
    "options": [
      "all",
      "reply"
    ]
  },
  {
    "key": "audio_group_trigger",
    "label": "群聊音频范围",
    "type": "select",
    "options": [
      "all",
      "reply"
    ]
  },
  {
    "key": "image_forward_mode",
    "label": "转发图片",
    "type": "select",
    "options": [
      "follow",
      "manual",
      "off"
    ]
  },
  {
    "key": "audio_forward_mode",
    "label": "转发音频",
    "type": "select",
    "options": [
      "follow",
      "manual",
      "off"
    ]
  },
  {
    "key": "media_require_model",
    "label": "必须使用指定媒体模型",
    "type": "switch",
    "hint": "必须配置图片／音频专用模型；不会回退至后台模型或主聊天模型"
  },
  {
    "key": "image_max_count",
    "label": "每条消息图片上限",
    "type": "number",
    "min": 0,
    "max": 4
  },
  {
    "key": "audio_max_count",
    "label": "每条消息音频上限",
    "type": "number",
    "min": 0,
    "max": 4
  },
  {
    "key": "image_max_edge",
    "label": "图片最长边（像素，0 不缩放）",
    "type": "number",
    "min": 0,
    "max": 8192
  },
  {
    "key": "image_max_mb",
    "label": "单张图片上限（MiB）",
    "type": "number",
    "min": 0,
    "max": 10
  },
  {
    "key": "audio_max_mb",
    "label": "单段音频上限（MiB）",
    "type": "number",
    "min": 0,
    "max": 10
  },
  {
    "key": "audio_max_seconds",
    "label": "音频时长上限（秒，0 不限）",
    "type": "number",
    "min": 0,
    "max": 3600,
    "hint": "0 不限制；启用时无法读取时长的音频会跳过。非 WAV 需要 ffprobe"
  },
  {
    "key": "image_detail",
    "label": "图片转述方式",
    "type": "select",
    "options": [
      "brief",
      "detailed"
    ]
  },
  {
    "key": "audio_detail",
    "label": "音频转述方式",
    "type": "select",
    "options": [
      "brief",
      "detailed"
    ]
  },
  {
    "key": "media_text_chars",
    "label": "随附文字字符上限",
    "type": "number",
    "min": 0,
    "max": 2000
  },
  {
    "key": "media_output_tokens",
    "label": "输出长度目标（Token）",
    "type": "number",
    "min": 0,
    "max": 8192,
    "hint": "仅提示词要求，不修改供应商，不保证实际输出上限"
  },
  {
    "key": "media_max_requests",
    "label": "每条消息请求上限",
    "type": "number",
    "min": 0,
    "max": 4
  },
  {
    "key": "media_timeout_seconds",
    "label": "单次请求超时（秒）",
    "type": "number",
    "min": 1,
    "max": 90
  },
  {
    "key": "media_cache_days",
    "label": "成功缓存天数（0 关闭）",
    "type": "number",
    "min": 0,
    "max": 365,
    "hint": "同会话按文件内容、模型、文字与参数复用；启用后按附件处理以支持精确重试"
  },
  {
    "key": "media_cache_entries",
    "label": "缓存容量（全插件条数）",
    "type": "number",
    "min": 0,
    "max": 10000
  },
  {
    "key": "media_daily_requests",
    "label": "每日多媒体请求上限",
    "type": "number",
    "min": 0,
    "max": 100000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "image_daily_requests",
    "label": "每日图片请求上限",
    "type": "number",
    "min": 0,
    "max": 100000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "audio_daily_requests",
    "label": "每日音频请求上限",
    "type": "number",
    "min": 0,
    "max": 100000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "image_daily_count",
    "label": "每日图片数量上限",
    "type": "number",
    "min": 0,
    "max": 100000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "audio_daily_seconds",
    "label": "每日音频秒数上限",
    "type": "number",
    "min": 0,
    "max": 8640000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "media_daily_tokens",
    "label": "每日多媒体 Token 预算",
    "type": "number",
    "min": 0,
    "max": 1000000000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "image_daily_tokens",
    "label": "每日图片 Token 预算",
    "type": "number",
    "min": 0,
    "max": 1000000000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "audio_daily_tokens",
    "label": "每日音频 Token 预算",
    "type": "number",
    "min": 0,
    "max": 1000000000,
    "hint": "0 不限；全局总额与会话额度同时检查，失败调用和手动重试也计数"
  },
  {
    "key": "media_token_reserve",
    "label": "单次预占 Token（估算）",
    "type": "number",
    "min": 1,
    "max": 1000000
  },
  {
    "key": "media_strict_budget",
    "label": "未知用量暂停后续调用",
    "type": "switch",
    "hint": "未返回用量时保留预占额度，暂停自动及手动调用；核对后可在预算卡片解除暂停"
  },
  {
    "key": "media_day_offset",
    "label": "预算时区 UTC 偏移（分钟）",
    "type": "number",
    "min": -720,
    "max": 840
  }
];
export const MEDIA_DEFAULTS = {"image_mode": "auto", "audio_mode": "auto", "image_group_trigger": "all", "audio_group_trigger": "all", "image_forward_mode": "follow", "audio_forward_mode": "follow", "media_require_model": false, "image_max_count": 4, "audio_max_count": 4, "image_max_edge": 0, "image_max_mb": 10, "audio_max_mb": 10, "audio_max_seconds": 0, "image_detail": "detailed", "audio_detail": "detailed", "media_text_chars": 2000, "media_output_tokens": 0, "media_max_requests": 4, "media_timeout_seconds": 30, "media_cache_days": 0, "media_cache_entries": 1000, "media_daily_requests": 0, "image_daily_requests": 0, "audio_daily_requests": 0, "image_daily_count": 0, "audio_daily_seconds": 0, "media_daily_tokens": 0, "image_daily_tokens": 0, "audio_daily_tokens": 0, "media_token_reserve": 4096, "media_strict_budget": false, "media_day_offset": 480};
export const MEDIA_LABELS = { auto: "自动", manual: "仅手动", off: "关闭", all: "全部消息", reply: "仅触发回复", follow: "跟随普通消息", brief: "简短", detailed: "详细" };
export const MEDIA_BASIC = new Set(["image_mode","audio_mode","image_group_trigger","audio_group_trigger","image_forward_mode","audio_forward_mode","media_require_model","media_daily_requests","media_daily_tokens","image_daily_count","audio_daily_seconds"]);
export const MEDIA_PRESETS = {
  current: { ...MEDIA_DEFAULTS },
  saving: { ...MEDIA_DEFAULTS, image_group_trigger: "reply", audio_group_trigger: "reply", image_forward_mode: "manual", audio_forward_mode: "manual", image_max_count: 2, audio_max_count: 1, audio_max_seconds: 60, image_max_edge: 1280, image_detail: "brief", audio_detail: "brief", media_output_tokens: 512, media_cache_days: 7 },
  manual: { ...MEDIA_DEFAULTS, image_mode: "manual", audio_mode: "manual" },
};
export function budgetCard(budget) {
  if (!budget) return "";
  const labels = { requests: "请求", images: "图片", seconds: "音频秒", tokens: "Token（含预占）" };
  return `<section class="media-budget" aria-label="多媒体预算"><h3>今日多媒体额度</h3><p class="f-hint">仅限插件多媒体调用 · UTC${budget.offset >= 0 ? "+" : ""}${Number(budget.offset) / 60} · 下次重置 ${esc(new Date(budget.reset_at * 1000).toLocaleString())}。Token 预占是估算，不能保证账单绝对不超额。</p>${(budget.buckets || []).map(bucket => {
    const total = Object.fromEntries(Object.keys(labels).map(k => [k, (bucket.usage || []).reduce((n, r) => n + Number(r[k] || 0), 0)]));
    const limits = { requests: bucket.limits.media_daily_requests, images: bucket.limits.image_daily_count, seconds: bucket.limits.audio_daily_seconds, tokens: bucket.limits.media_daily_tokens };
    const unknown = bucket.unresolved ?? (bucket.usage || []).reduce((n,r) => n + Number(r.unknown || 0), 0);
    return `<h4>${bucket.scope ? "本会话" : "全插件总额"}</h4><div class="media-metrics">${Object.entries(labels).map(([k,label]) => `<div><span>${label}</span><strong>${total[k].toLocaleString()} / ${limits[k] ? Number(limits[k]).toLocaleString() : "不限"}</strong><small>剩余 ${limits[k] ? Math.max(0,limits[k]-total[k]).toLocaleString() : "不限"}</small></div>`).join("")}</div>${unknown ? `<p class="config-notice">${unknown} 次用量未知，已保留预占额度。</p>` : ""}<details><summary>图片／音频独立限额</summary>${["image","audio"].map(kind => `<p>${kind === "image" ? "图片" : "音频"}：请求 ${bucket.limits[kind+"_daily_requests"] || "不限"}，Token ${bucket.limits[kind+"_daily_tokens"] || "不限"}</p>`).join("")}</details>`;
  }).join("")}<p class="f-hint">缓存复用 ${(budget.decisions || []).filter(x => x.reason === "cache_hit").reduce((n,x) => n + x.count,0)} 次，避免同等次数的插件调用；不估造节省 Token。</p><button class="btn" type="button" data-review-budget>已核对未知调用，解除暂停</button></section>`;
}
