import { $, beginLoad, RETRY_LOADERS, state } from "./state.js";
import { esc, errorPanel, refreshIcons, skeleton, timeAgo, toast } from "./utils.js";
import { bridge, safe } from "./api.js";
import { ask, markSaved } from "./dialogs.js";

/* ---------- config forms ---------- */
const GLOBAL_FIELDS = [
  {
    section: "基础",
    items: [
      { key: "enable_private_memory", label: "启用私聊记忆", type: "switch", hint: "默认开启，保存用户与助手的私聊轮次。关闭后停止捕获、召回与巩固，已有数据需单独清空" },
      { key: "enable_group_memory", label: "启用群聊记忆", type: "switch", hint: "默认开启：会被动记录机器人收到的群消息，无需 @ 或触发回复；指令和忽略规则仍生效。后台巩固会把原文发送给所选模型。关闭后停止捕获、召回与巩固，已有数据需单独清空" },
      { key: "background_llm_provider", label: "后台小模型", type: "provider", hint: "用于记忆巩固及未单独指定的媒体处理；留空使用当前会话模型。实际调用及重试记录在用量统计中" },
      { key: "image_llm_provider", label: "图片处理模型", type: "provider", fallback: "继承后台模型", hint: "选择 AstrBot 中支持 image 输入的模型，描述图片与识别文字" },
      { key: "audio_llm_provider", label: "音频处理模型", type: "provider", fallback: "继承后台模型", hint: "选择支持 audio 输入的聊天模型，转写音频。留空继承后台模型，再回退当前会话模型" },
    ],
  },
  {
    section: "召回",
    items: [
      { key: "recall_max_chars", label: "召回总字符预算", type: "number", min: 512, max: 20000, hint: "三层记忆共用预算，默认 6000 字符" },
      { key: "recall_top_k", label: "线索召回条数", type: "number", min: 1, max: 30 },
      { key: "recall_core_top_k", label: "常驻记忆条数", type: "number", min: 0, max: 10, hint: "每次对话固定注入的稳定认知，0 关闭" },
      { key: "recall_recent_turns", label: "群聊近因条数", type: "number", min: 0, max: 20, hint: "群聊召回时附带最近原文，0 关闭" },
    ],
  },
  {
    section: "巩固与遗忘",
    items: [
      { key: "consolidation_scan_interval_minutes", label: "扫描周期（分钟）", type: "number", min: 1, max: 1440, hint: "积压时单会话每轮最多处理 3 批，每批通常调用 1 次模型；输出校验失败时最多重试 1 次" },
      { key: "consolidation_count_threshold_private", label: "私聊触发阈值（轮）", type: "number", min: 1, max: 500 },
      { key: "consolidation_count_threshold_group", label: "群聊触发阈值（条）", type: "number", min: 1, max: 1000 },
      { key: "consolidation_idle_hours", label: "静默触发（小时）", type: "number", min: 1, max: 168 },
      { key: "raw_retention_days", label: "原文保留天数", type: "number", min: 0, max: 365, hint: "0 不按时间清理，仍受容量限制" },
      { key: "decay_rate_semantic", label: "语义衰减系数", type: "float", hint: "每自然日强度保留比例 0-1，按距上次激活的实际天数折算" },
      { key: "decay_rate_insight", label: "洞察衰减系数", type: "float", hint: "洞察几乎不遗忘，接近 1" },
    ],
  },
  {
    section: "跨会话桥接",
    items: [
      { key: "enable_cross_scope_bridge", label: "启用桥接", type: "switch", hint: "群聊中的自我陈述写入用户私聊记忆（仍需用户授权）" },
      { key: "bridge_max_sensitivity", label: "敏感度上限", type: "select", options: ["low", "medium", "high"], hint: "low 仅日常事实；medium 含一般隐私；high 不限制" },
    ],
  },
];

const SCOPE_FIELDS = [
  { key: "recall_max_chars", label: "召回总字符预算", type: "inherit-number" },
  { key: "enabled", label: "启用本会话记忆", type: "inherit-switch", hint: "关闭后停止本会话的捕获、召回与巩固，已有数据需单独清空；全局类型开关关闭时，本项不能重新开启记忆" },
  { key: "recall_top_k", label: "线索召回条数", type: "inherit-number" },
  { key: "recall_core_top_k", label: "常驻记忆条数", type: "inherit-number" },
  { key: "recall_recent_turns", label: "群聊近因条数", type: "inherit-number", groupOnly: true },
  { key: "consolidation_count_threshold", label: "巩固触发阈值", type: "inherit-number", hint: "未抽取原文达到该数量后可触发巩固；积压和重试可能产生多次模型调用" },
  { key: "consolidation_idle_hours", label: "静默触发（小时）", type: "inherit-number" },
  { key: "bridge_enabled", label: "允许自我陈述桥接", type: "inherit-switch", groupOnly: true, hint: "叠加在用户授权之上" },
  { key: "bridge_max_sensitivity", label: "敏感度上限", type: "inherit-select", options: ["low", "medium", "high"], groupOnly: true },
];

function fieldRow(f, value, inheritValue, inheritable, effectiveValue) {
  const hint = f.hint ? `<div class="f-hint">${esc(f.hint)}</div>` : "";
  let control = "";
  if (!inheritable) {
    if (f.type === "switch") {
      control = `<label class="switch"><input type="checkbox" data-cfg="${f.key}" ${value ? "checked" : ""} /><span class="slider"></span></label>`;
    } else if (f.type === "select" || f.type === "provider") {
      const opts = (f.type === "provider" ? state.providers : f.options) || [];
      control = `<select class="f-input" data-cfg="${f.key}">${
        f.type === "provider" ? `<option value="">（${f.fallback || "使用当前对话模型"}）</option>` : ""
      }${opts.map((o) => `<option value="${esc(o)}" ${value === o ? "selected" : ""}>${esc(f.type === "provider" ? o : { low: "low 仅日常事实", medium: "medium 含一般隐私", high: "high 不限制" }[o] || o)}</option>`).join("")}</select>`;
    } else {
      control = `<input type="number" class="f-input" data-cfg="${f.key}" value="${value ?? ""}" step="${f.type === "float" ? "0.01" : "1"}" min="${f.min ?? 0}" max="${f.max ?? (f.type === "float" ? 1 : 100000)}" />`;
    }
  } else {
    // Inherit-style control: empty value = follow global
    if (f.type === "inherit-switch") {
      const opts = [
        ["", "跟随全局"],
        ["true", "开启"],
        ["false", "关闭"],
      ];
      const cur = value === undefined || value === null ? "" : String(value);
      control = `<select class="f-input" data-cfg="${f.key}">${opts.map(([v, t]) => `<option value="${v}" ${cur === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
    } else if (f.type === "inherit-select") {
      const opts = ["", ...f.options];
      control = `<select class="f-input" data-cfg="${f.key}">${opts.map((o) => `<option value="${o}" ${value === o || (value === undefined && o === "") ? "selected" : ""}>${o === "" ? `跟随全局（当前：${esc(inheritValue)}）` : esc(o)}</option>`).join("")}</select>`;
    } else {
      control = `<input type="number" class="f-input" data-cfg="${f.key}" value="${value ?? ""}" min="${f.key === "recall_max_chars" ? 512 : 0}" max="${f.key === "recall_max_chars" ? 20000 : 1000}" placeholder="${inheritValue ?? "继承全局"}" />`;
    }
  }
  const id = `${inheritable ? "scope" : "global"}-${f.key}`;
  control = control.replace(/<(input|select) /, `<$1 id="${id}" `);
  return `<div class="field"><div style="flex:1;min-width:0"><label class="f-label" for="${id}">${esc(f.label)}</label>${hint}${inheritable ? `<div class="effective-value">当前生效：${esc(typeof effectiveValue === "boolean" ? (effectiveValue ? "开启" : "关闭") : effectiveValue ?? inheritValue ?? "默认")} · ${value === undefined ? "来自全局" : "会话覆盖"}</div>` : ""}</div>${control}</div>`;
}

export async function loadScopeConfigTab() {
  const request = beginLoad();
  const global = state.tab === "global";
  $("content").innerHTML = skeleton(3);
  const [cfgRes, gRes] = await Promise.all([
    global ? Promise.resolve({ override: {}, effective: {} }) : safe("加载会话配置", () => bridge.apiGet("scope-config", request.scope), { silent: true }),
    safe("加载全局配置", () => bridge.apiGet("config"), { silent: true }),
  ]);
  if (!request.current()) return;
  if (!cfgRes || !gRes) { $("content").innerHTML = errorPanel("配置", "请求失败，请重试", "settings"); refreshIcons(); return; }
  state.scopeOverride = cfgRes.override || {};
  state.globalConfig = gRes.config || {};
  state.providers = gRes.providers || [];
  const ov = state.scopeOverride, g = state.globalConfig;
  if (global) {
    const rows = GLOBAL_FIELDS.map(section => `<section><h3>${esc(section.section)}</h3>${section.items.map(f => fieldRow(f, g[f.key], null, false)).join("")}</section>`).join("");
    $("content").innerHTML = `<form class="form-card" id="global-config-form"><h2>全局默认配置</h2><p class="form-sub">未覆盖的会话继承这里的设置；私聊和群聊总开关优先于会话设置。关闭不会清除历史数据。</p>${rows}<div class="form-actions sticky-save"><span class="save-state"></span><button class="btn primary" id="save-global-cfg" type="submit">保存全局配置</button></div></form>`;
    const form = $("global-config-form"); markSaved(form); form.onsubmit = e => { e.preventDefault(); saveGlobalConfig(); };
  } else {
    const isGroup = request.scope.scope_type === "group";
    const fields = SCOPE_FIELDS.filter(f => isGroup || !f.groupOnly);
    const rows = fields.map(f => {
      const baseKey = f.key === "enabled" ? `enable_${request.scope.scope_type}_memory` : f.key === "bridge_enabled" ? "enable_cross_scope_bridge" : f.key === "consolidation_count_threshold" ? `consolidation_count_threshold_${request.scope.scope_type}` : f.key;
      return fieldRow(f, ov[f.key], g[baseKey], true, cfgRes.effective?.[f.key] ?? ov[f.key] ?? g[baseKey]);
    }).join("");
    $("content").innerHTML = `<form class="form-card" id="scope-config-form" data-scope-type="${esc(request.scope.scope_type)}" data-scope-key="${esc(request.scope.scope_key)}"><h2>当前会话设置</h2><p class="form-sub">留空或选择「跟随全局」恢复继承。保存后更新实际生效值。</p>${g[`enable_${request.scope.scope_type}_memory`] === false ? '<p class="config-notice">全局类型开关已关闭，本会话开关无法单独开启记忆。</p>' : ""}${rows}<div class="form-actions sticky-save"><span class="save-state"></span><button class="btn" id="reset-scope-cfg" type="button">恢复继承</button><button class="btn primary" id="save-scope-cfg" type="submit">保存会话设置</button></div></form>`;
    const form = $("scope-config-form"); markSaved(form); form.onsubmit = e => { e.preventDefault(); saveScopeConfig(); };
  }
  refreshIcons();
}

function collectScopeOverride() {
  const override = {};
  for (const f of SCOPE_FIELDS) {
    const el = document.querySelector(`#scope-config-form [data-cfg="${f.key}"]`);
    if (!el) continue;
    if (f.type === "inherit-switch") {
      if (el.value !== "") override[f.key] = el.value === "true";
    } else if (f.type === "inherit-select") {
      if (el.value !== "") override[f.key] = el.value;
    } else if (f.type === "inherit-number") {
      if (el.value !== "") override[f.key] = Number(el.value);
    }
  }
  return override;
}

export async function saveScopeConfig() {
  const form = $("scope-config-form");
  if (!form || form.dataset.saving === "true" || !form.reportValidity()) return;
  const scope = { scope_type: form.dataset.scopeType, scope_key: form.dataset.scopeKey };
  const override = collectScopeOverride();
  form.dataset.saving = "true"; form.inert = true;
  const result = await safe("保存会话配置", () => bridge.apiPost("scope-config/update", { ...scope, override }));
  form.dataset.saving = "false"; form.inert = false;
  if (result === null || !form.isConnected) return;
  markSaved(form); toast("会话设置已保存"); await loadScopeConfigTab(); document.dispatchEvent(new Event("memoir:changed"));
}
export async function resetScopeConfig() {
  const form = $("scope-config-form");
  if (!form || form.dataset.saving === "true") return;
  const scope = { scope_type: form.dataset.scopeType, scope_key: form.dataset.scopeKey };
  if (!await ask("恢复继承？", "移除本会话的全部覆盖项，未保存修改也将丢弃。", "恢复继承")) return;
  form.dataset.saving = "true"; form.inert = true;
  const result = await safe("恢复继承", () => bridge.apiPost("scope-config/update", { ...scope, override: {} }));
  form.dataset.saving = "false"; form.inert = false;
  if (result === null || !form.isConnected) return;
  markSaved(form); toast("已恢复继承"); await loadScopeConfigTab(); document.dispatchEvent(new Event("memoir:changed"));
}

function collectGlobalConfig() {
  const payload = {};
  for (const sec of GLOBAL_FIELDS) {
    for (const f of sec.items) {
      const el = document.querySelector(`#global-config-form [data-cfg="${f.key}"]`);
      if (!el) continue;
      if (f.type === "switch") payload[f.key] = el.checked;
      else if (f.type === "number" || f.type === "float") payload[f.key] = Number(el.value);
      else payload[f.key] = el.value;
    }
  }
  return payload;
}

export async function saveGlobalConfig() {
  const form = $("global-config-form");
  if (!form || form.dataset.saving === "true" || !form.reportValidity()) return;
  const payload = collectGlobalConfig();
  form.dataset.saving = "true"; form.inert = true;
  const res = await safe("保存全局配置", () => bridge.apiPost("config/update", payload));
  form.dataset.saving = "false"; form.inert = false;
  if (res === null || !form.isConnected) return;
  markSaved(form); toast("全局设置已保存"); await loadScopeConfigTab(); document.dispatchEvent(new Event("memoir:changed"));
}

export async function loadConsentPage() {
  beginLoad();
  $("content").innerHTML = '<div class="form-card"><h2>桥接授权记录</h2><p class="form-sub">用户通过 /memoir consent on 授权后显示在这里；转发引用不参与桥接。</p><div id="consent-box"></div></div>';
  await loadConsents();
}
export async function loadConsents() {
  const box = $("consent-box");
  if (!box) return;
  const res = await safe("加载授权记录", () => bridge.apiGet("consents"), { silent: true });
  if (!box.isConnected) return;
  if (!res) {
    box.innerHTML = errorPanel("加载授权记录", "请求失败", "consents");
    refreshIcons();
    return;
  }
  const items = res.items || [];
  box.innerHTML = items.length
    ? items
        .map(
          (c) => `<div class="consent-row">
            <div class="who">
              <div class="pid">${esc(c.sender_id)}</div>
              <div class="plat">${esc(c.platform)}</div>
            </div>
            <span class="consent-state ${c.enabled ? "on" : "off"}">${c.enabled ? "已授权" : "未授权"}</span>
            <label class="switch"><input type="checkbox" data-consent="${esc(c.platform)}|${esc(c.sender_id)}" ${c.enabled ? "checked" : ""} /><span class="slider"></span></label>
            <span class="time">${timeAgo(c.updated_at)}</span>
          </div>`,
        )
        .join("")
    : `<p style="color:var(--text-faint);font-size:12.5px;margin:0">暂无授权记录</p>`;
  refreshIcons();
}
RETRY_LOADERS.consents = () => loadConsents();

let processingTimer;
export const stopProcessing = () => clearTimeout(processingTimer);
export async function loadProcessingStatus(force = false) {
  stopProcessing();
  const box = $("processing-status");
  if (!box || !state.scope || state.tab !== "processing" || document.hidden) return;
  if (!force && box.contains(document.activeElement)) { processingTimer = setTimeout(() => loadProcessingStatus(), 5000); return; }
  const scope = { ...state.scope }, version = state.loadVersion;
  const requestId = (Number(box.dataset.request) || 0) + 1;
  box.dataset.request = String(requestId);
  const result = await safe("加载处理状态", () => bridge.apiGet("processing", scope), { silent: true });
  if (!box.isConnected || version !== state.loadVersion || Number(box.dataset.request) !== requestId) return;
  if (!result) { box.innerHTML = errorPanel("处理状态", "请求失败，请重试", "processing"); refreshIcons(); return; }
  const names = { pending: "排队", running: "处理中", failed: "失败", complete: "完成" };
  const kinds = { media: "媒体解析", forward: "转发解析", consolidation: "记忆巩固" };
  const jobs = (result.items || result.failures).filter(job => !state.filters.task_status || job.status === state.filters.task_status);
  box.innerHTML = `<div class="form-title">处理任务<button class="btn" id="refresh-processing">刷新</button></div><p class="form-sub">最早待巩固原文：${Math.floor(result.oldest_pending_seconds / 60)} 分钟 · 最近 100 条任务；有活动任务时每 5 秒刷新</p><div class="processing-counts">${result.counts.map(c => `<span class="subject-chip">${kinds[c.kind] || c.kind} · ${names[c.status] || c.status} ${c.count}</span>`).join("") || "暂无任务"}</div>${jobs.map(job => {
    const error = String(job.error || "");
    let advice = "请检查模型或适配器设置；不可重试时请重新发送原消息。";
    if (/timeout|deadline|exceeded.*seconds/i.test(error)) advice = "处理超时，请检查网络或模型服务后重试。";
    else if (/unsupported|capabilit|preview/i.test(error)) advice = "模型能力不足或平台只提供预览。调整模型能力或重新发送正文。";
    else if (/budget|limit|MiB|truncated/i.test(error)) advice = "内容超过处理限制，请拆分消息或缩小附件。";
    else if (/changed|disabled/i.test(error)) advice = "会话设置已变化，请确认记忆开关后重试。";
    else if (/instance|fetch|unavailable/i.test(error)) advice = "暂时无法获取来源，请检查原适配器实例和消息是否仍可访问。";
    return `<article class="processing-failure"><div><strong>${esc(kinds[job.kind] || job.kind)} #${job.id}</strong><span class="status-chip">${esc(names[job.status] || "失败")}</span>${error ? `<p class="task-advice">${advice}</p><details><summary>查看原始原因</summary><p>${esc(error)}</p></details>` : ""}<small>尝试 ${job.attempts} 次 · ${timeAgo(job.updated_at)}</small></div><div>${job.raw_id ? `<button class="btn" data-open-event="${job.raw_id}">查看来源</button>` : ""}${job.status === "failed" || job.retryable ? `<button class="btn" data-retry-work="${job.id}" data-scope-type="${esc(scope.scope_type)}" data-scope-key="${esc(scope.scope_key)}" ${job.retryable ? "" : "disabled"}>${job.retryable ? "重试" : "不可重试"}</button>` : ""}</div></article>`;
  }).join("") || '<p class="form-sub">没有符合筛选条件的任务</p>'}`;
  refreshIcons();
  if (result.counts.some(c => ["pending", "running"].includes(c.status) && c.count)) processingTimer = setTimeout(() => loadProcessingStatus(), 5000);
}
export function loadProcessingPage() {
  beginLoad();
  $("content").innerHTML = '<div class="form-card" id="processing-status">正在加载…</div>';
  return loadProcessingStatus();
}
RETRY_LOADERS.processing = loadProcessingStatus;
