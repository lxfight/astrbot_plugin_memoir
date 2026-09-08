import { $, beginLoad, RETRY_LOADERS, state } from "./state.js";
import { esc, errorPanel, refreshIcons, skeleton, stagger, timeAgo, toast } from "./utils.js";
import { bridge, safe } from "./api.js";
import { renderDetailHead } from "./sidebar.js";

/* ---------- config forms ---------- */
const GLOBAL_FIELDS = [
  {
    section: "基础",
    items: [
      { key: "enable_private_memory", label: "启用私聊记忆", type: "switch", hint: "默认开启，保存用户与助手的私聊轮次。关闭后停止捕获、召回与巩固，已有数据需单独清空" },
      { key: "enable_group_memory", label: "启用群聊记忆", type: "switch", hint: "默认开启：会被动记录机器人收到的群消息，无需 @ 或触发回复；指令和忽略规则仍生效。后台巩固会把原文发送给所选模型。关闭后停止捕获、召回与巩固，已有数据需单独清空" },
      { key: "background_llm_provider", label: "后台小模型", type: "provider", hint: "巩固会发送本批原文与相关记忆，留空则用当前会话模型。巩固可能分批调用和重试；每条受支持媒体通常额外调用 1 次描述模型，手动重试会再次调用。费用按提供商计费" },
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

function fieldRow(f, value, inheritValue, inheritable) {
  const hint = f.hint ? `<div class="f-hint">${esc(f.hint)}</div>` : "";
  let control = "";
  if (!inheritable) {
    if (f.type === "switch") {
      control = `<label class="switch"><input type="checkbox" data-cfg="${f.key}" ${value ? "checked" : ""} /><span class="slider"></span></label>`;
    } else if (f.type === "select" || f.type === "provider") {
      const opts = (f.type === "provider" ? state.providers : f.options) || [];
      control = `<select class="f-input" data-cfg="${f.key}">${
        f.type === "provider" ? `<option value="">（使用当前对话模型）</option>` : ""
      }${opts.map((o) => `<option value="${esc(o)}" ${value === o ? "selected" : ""}>${esc(f.type === "provider" ? o : { low: "low 仅日常事实", medium: "medium 含一般隐私", high: "high 不限制" }[o] || o)}</option>`).join("")}</select>`;
    } else {
      control = `<input type="number" class="f-input" data-cfg="${f.key}" value="${value ?? ""}" step="${f.type === "float" ? "0.01" : "1"}" min="${f.min ?? 0}" max="${f.max ?? 100000}" />`;
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
      control = `<input type="number" class="f-input" data-cfg="${f.key}" value="${value ?? ""}" placeholder="${inheritValue ?? "继承全局"}" />`;
    }
  }
  const id = `${inheritable ? "scope" : "global"}-${f.key}`;
  control = control.replace(/<(input|select) /, `<$1 id="${id}" `);
  return `<div class="field"><div style="flex:1;min-width:0"><label class="f-label" for="${id}">${esc(f.label)}</label>${hint}</div>${control}</div>`;
}

export async function loadScopeConfigTab() {
  const request = beginLoad();
  $("content").innerHTML = skeleton(3);
  const [cfgRes, gRes] = await Promise.all([
    safe(
      "加载会话配置",
      () =>
        bridge.apiGet("scope-config", {
          ...request.scope,
        }),
      { silent: true },
    ),
    safe("加载全局配置", () => bridge.apiGet("config"), { silent: true }),
  ]);
  if (!request.current()) return;
  if (!cfgRes || !gRes) {
    $("content").innerHTML = errorPanel("加载配置", "请求失败，请检查后端状态后重试", "settings");
    refreshIcons();
    return;
  }
  state.scopeOverride = cfgRes.override || {};
  state.globalConfig = gRes.config || {};
  state.providers = gRes.providers || [];
  const ov = state.scopeOverride;
  const g = state.globalConfig;
  const isGroup = state.scope.scope_type === "group";
  const fields = SCOPE_FIELDS.filter((f) => isGroup || !f.groupOnly);
  const rows = fields
    .map((f, i) => fieldRow(f, ov[f.key], g[f.key] ?? "", true))
    .join("");
  const globalRows = GLOBAL_FIELDS.map((gsec, si) => {
    const gRows = gsec.items.map((f) => fieldRow(f, g[f.key], null, false)).join("");
    return `<div class="form-section-sub"${stagger(si, 0)}>${esc(gsec.section)}</div>${gRows}`;
  }).join("");
  $("content").innerHTML = `
    <div class="form-card" id="processing-status">正在加载处理状态…</div>
    <div class="form-card" id="scope-config-form" data-scope-type="${esc(request.scope.scope_type)}" data-scope-key="${esc(request.scope.scope_key)}"${stagger(0)}>
      <div class="form-title"><i data-lucide="sliders-horizontal"></i>会话覆盖配置</div>
      <p class="form-sub">仅对此会话生效；留空/「跟随全局」表示使用下方全局默认值</p>
      ${rows}
      <div class="form-actions">
        <button class="btn" id="reset-scope-cfg"><i data-lucide="rotate-ccw"></i>恢复继承</button>
        <button class="btn primary" id="save-scope-cfg"><i data-lucide="check"></i>保存覆盖</button>
      </div>
    </div>
    <div class="form-card" id="global-config-form"${stagger(1)}>
      <div class="form-title"><i data-lucide="globe"></i>全局默认配置</div>
      <p class="form-sub">未覆盖的会话使用这些默认值；私聊和群聊总开关对所有对应会话生效，全局关闭优先</p>
      ${globalRows}
      <div class="form-actions">
        <button class="btn primary" id="save-global-cfg"><i data-lucide="check"></i>保存全局配置</button>
      </div>
    </div>
    <div class="form-card"${stagger(2)}>
      <div class="form-title"><i data-lucide="shield-check"></i>桥接授权记录</div>
      <p class="form-sub">用户通过聊天指令 <code>/memoir consent on</code> 授权后出现在这里</p>
      <div id="consent-box">${skeleton(2)}</div>
    </div>`;
  refreshIcons();
  loadConsents();
  loadProcessingStatus();
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
  if (!form) return;
  const scope = { scope_type: form.dataset.scopeType, scope_key: form.dataset.scopeKey };
  const version = state.loadVersion;
  const override = collectScopeOverride();
  const res = await safe("保存会话配置", () =>
    bridge.apiPost("scope-config/update", { ...scope, override }),
  );
  if (res === null || version !== state.loadVersion) return;
  state.scopeOverride = override;
  toast(override && Object.keys(override).length ? "会话配置已保存" : "已恢复继承全局");
  renderDetailHead();
}

export async function resetScopeConfig() {
  const scope = { ...state.scope };
  const version = state.loadVersion;
  const res = await safe("恢复配置", () =>
    bridge.apiPost("scope-config/update", { ...scope, override: {} }),
  );
  if (res === null || version !== state.loadVersion) return;
  state.scopeOverride = {};
  toast("已恢复继承全局配置");
  renderDetailHead();
  loadScopeConfigTab();
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
  const payload = collectGlobalConfig();
  const res = await safe("保存全局配置", () => bridge.apiPost("config/update", payload));
  if (res === null) return;
  toast(`已保存 ${res.updated?.length ?? 0} 项全局配置`);
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

export async function loadProcessingStatus() {
  const box = $("processing-status");
  if (!box || !state.scope) return;
  const scope = { ...state.scope };
  const version = state.loadVersion;
  const requestId = (Number(box.dataset.request) || 0) + 1;
  box.dataset.request = String(requestId);
  const result = await safe("加载处理状态", () => bridge.apiGet("processing", scope), { silent: true });
  if (!box.isConnected || version !== state.loadVersion || Number(box.dataset.request) !== requestId) return;
  if (!result) {
    box.innerHTML = errorPanel("处理状态", "暂时无法读取，请重试", "processing");
    refreshIcons();
    return;
  }
  const names = { pending: "排队", running: "处理中", failed: "失败", complete: "完成" };
  box.innerHTML = `<div class="form-title"><i data-lucide="activity"></i>处理状态<button class="btn" id="refresh-processing">刷新状态</button></div>
    <p class="form-sub">最早积压：${Math.floor(result.oldest_pending_seconds / 60)} 分钟 · 媒体队列最多 32 条，同时处理 2 条</p>
    <div class="processing-counts">${result.counts.map((c) => `<span class="subject-chip">${c.kind === "media" ? "媒体" : "巩固"} ${esc(names[c.status] || c.status)} ${c.count}</span>`).join("") || "暂无后台任务"}</div>
    ${result.failures.map((f) => `<div class="processing-failure"><div><strong>${f.kind === "media" ? "媒体解析" : "记忆巩固"} #${f.id}</strong><p>${esc(f.error)}</p><small>尝试 ${f.attempts} 次 · ${timeAgo(f.updated_at)}</small></div><button class="btn" data-retry-work="${f.id}" data-scope-type="${esc(scope.scope_type)}" data-scope-key="${esc(scope.scope_key)}" ${f.retryable ? "" : "disabled"}>${f.retryable ? "重试" : "来源已过期"}</button></div>`).join("")}`;
  refreshIcons();
}
RETRY_LOADERS.processing = loadProcessingStatus;
