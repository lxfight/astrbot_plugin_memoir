import { $, beginLoad, RETRY_LOADERS, state } from "./state.js";
import { esc, errorPanel, skeleton } from "./utils.js";
import { bridge, safe } from "./api.js";

const PURPOSES = { consolidation: "记忆巩固", media_image: "图片描述", media_audio: "音频转写", media_mixed: "混合媒体" };
const STATUSES = { running: "调用中", complete: "完成", error: "失败", cancelled: "已取消", interrupted: "进程中断" };
const SERIES = [["input_other", "未缓存输入", "var(--accent)"], ["input_cached", "缓存输入", "var(--blue)"], ["output", "输出", "var(--amber)"]];
const providers = new Set();
const number = value => value == null ? "—" : Number(value).toLocaleString("zh-CN");
const total = row => row.input_other == null ? null : row.input_other + row.input_cached + row.output;

function renderChart(data) {
  const rows = new Map(data.daily.map(row => [row.day, row]));
  const days = [];
  for (let stamp = data.since; stamp < data.until; stamp += 86400) {
    const day = new Date((stamp + data.offset * 60) * 1000).toISOString().slice(0, 10);
    days.push({ day, calls: 0, reported_calls: 0, input_other: 0, input_cached: 0, output: 0, ...rows.get(day) });
  }
  const max = Math.max(1, ...days.map(total));
  const step = 920 / days.length;
  const bars = days.map((row, index) => {
    let y = 210;
    const description = `${row.day} · ${number(row.calls)} 次调用 · 未缓存输入 ${number(row.input_other)} · 缓存输入 ${number(row.input_cached)} · 输出 ${number(row.output)} · 已报告合计 ${number(total(row))} · 未知用量 ${number(row.calls - row.reported_calls)} 次`;
    const rectangles = SERIES.map(([key, , color]) => {
      const height = row[key] / max * 180; y -= height;
      return `<rect x="${60 + index * step + step * .15}" y="${y}" width="${step * .7}" height="${height}" fill="${color}"/>`;
    }).join("");
    return `<g tabindex="0" role="img" aria-label="${esc(description)}" data-day="${esc(description)}"><title>${esc(description)}</title><rect x="${60 + index * step}" y="25" width="${step}" height="185" fill="transparent"/>${rectangles}</g>`;
  }).join("");
  const labels = days.map((row, index) => index === 0 || index === days.length - 1 || index % Math.ceil(days.length / 6) === 0 ? `<text x="${60 + (index + .5) * step}" y="235" text-anchor="middle">${row.day.slice(5)}</text>` : "").join("");
  return `<div class="usage-chart-scroll"><svg class="usage-chart" viewBox="0 0 1010 250" aria-label="每日 token 消耗堆叠柱状图">${[0, .5, 1].map(fraction => `<line x1="60" x2="980" y1="${210 - fraction * 180}" y2="${210 - fraction * 180}"/><text x="52" y="${214 - fraction * 180}" text-anchor="end">${number(Math.round(max * fraction))}</text>`).join("")}${bars}${labels}</svg></div><p id="usage-day" class="usage-note" aria-live="polite">点击、悬停或聚焦日期柱，查看当天精确数值。日期按浏览器时区显示。</p>`;
}

function renderGroups(data, key, title) {
  return `<section class="usage-panel"><h3>${title}</h3><div class="usage-table-scroll"><table class="usage-table"><thead><tr><th>${key === "purpose" ? "任务" : "提供商"}</th><th>调用 / 未知</th><th>未缓存输入</th><th>缓存输入</th><th>输出</th><th>已报告合计</th></tr></thead><tbody>${data[key].map(row => `<tr><td>${esc(PURPOSES[row[key]] || row[key])}</td><td>${number(row.calls)} / ${number(row.calls - row.reported_calls)}</td>${SERIES.map(([name]) => `<td>${number(row[name])}</td>`).join("")}<td>${number(total(row))}</td></tr>`).join("") || '<tr><td colspan="6">暂无调用记录</td></tr>'}</tbody></table></div></section>`;
}

export async function loadUsage(before = 0, previous = []) {
  const request = beginLoad();
  const filters = { ...state.filters };
  const params = { days: filters.days || 30, offset: -new Date().getTimezoneOffset(), provider: filters.provider || "", purpose: filters.purpose || "", before };
  if (filters.usage_scope) {
    const split = filters.usage_scope.indexOf("|");
    params.scope_type = filters.usage_scope.slice(0, split); params.scope_key = filters.usage_scope.slice(split + 1);
  }
  $("content").innerHTML = skeleton(3);
  const data = await safe("加载用量统计", () => bridge.apiGet("usage", params));
  if (!request.current()) return;
  if (!data) { $("content").innerHTML = errorPanel("加载用量统计", "请重试，或检查插件连接。", "usage"); return; }
  data.provider_id.forEach(row => providers.add(row.provider_id));
  if (filters.provider) providers.add(filters.provider);
  const options = (items, selected) => items.map(([value, label]) => `<option value="${esc(value)}" ${String(value) === String(selected || "") ? "selected" : ""}>${esc(label)}</option>`).join("");
  $("filters").innerHTML = `<label>时间范围<select data-filter="days">${options([7, 30, 90, 365].map(day => [day, `最近 ${day} 天`]), params.days)}</select></label><label>会话<select data-filter="usage_scope">${options([["", "全部会话"], ...state.scopes.map(scope => [`${scope.scope_type}|${scope.scope_key}`, `${scope.scope_type === "group" ? "群聊" : "私聊"} · ${scope.scope_key}`])], filters.usage_scope)}</select></label><label>提供商<select data-filter="provider">${options([["", "全部提供商"], ...[...providers].sort().map(id => [id, id])], filters.provider)}</select></label><label>任务<select data-filter="purpose">${options([["", "全部任务"], ...Object.entries(PURPOSES)], filters.purpose)}</select></label>`;
  const totals = data.totals;
  $("content").innerHTML = `<div class="usage-page">
    <div class="usage-kpis">${[["已报告总 token", total(totals)], ...SERIES.map(([key, label]) => [label, totals[key]]), ["调用次数", totals.calls], ["未知用量的调用", totals.calls - totals.reported_calls]].map(([label, value], index) => `<div class="usage-kpi ${index === 0 ? "usage-primary" : ""}"><span>${label}</span><strong>${number(value)}</strong></div>`).join("")}</div>
    <section class="usage-panel"><div class="usage-heading"><h3>消耗趋势</h3><div class="usage-legend">${SERIES.map(([, label, color]) => `<span><i style="background:${color}"></i>${label}</span>`).join("")}</div></div>${totals.reported_calls ? renderChart(data) : '<p class="usage-empty">此时间范围内暂无已报告的 token 用量。开始调用后，统计会自动积累。</p>'}</section>
    <div class="usage-groups">${renderGroups(data, "provider_id", "按提供商")}${renderGroups(data, "purpose", "按任务")}</div>
    <section class="usage-panel"><div class="usage-heading"><h3>调用明细</h3><span class="usage-note">${number(totals.failed_calls)} 次失败、取消或中断 · 每页最多 50 条</span></div><div class="usage-table-scroll"><table class="usage-table usage-details"><thead><tr><th>时间 / 会话</th><th>提供商 / 模型</th><th>任务</th><th>未缓存输入</th><th>缓存输入</th><th>输出</th><th>总 token</th><th>耗时</th><th>状态</th></tr></thead><tbody>${data.items.map(row => `<tr><td>${esc(new Date(row.created_at * 1000).toLocaleString("zh-CN"))}<small>${esc(row.scope_type === "group" ? "群聊" : "私聊")} · ${esc(row.scope_key)}</small></td><td>${esc(row.provider_id)}<small>${esc(row.model)}</small></td><td>${esc(PURPOSES[row.purpose] || row.purpose)}</td>${SERIES.map(([key]) => `<td>${number(row[key])}</td>`).join("")}<td>${number(total(row))}${row.input_other == null ? '<small>未知</small>' : ""}</td><td>${row.duration_ms == null ? "—" : `${number(row.duration_ms)} ms`}</td><td>${esc(STATUSES[row.status] || row.status)}${row.error_type ? `<small>${esc(row.error_type)}</small>` : ""}</td></tr>`).join("") || '<tr><td colspan="9">暂无调用记录</td></tr>'}</tbody></table></div><div class="usage-pagination"><button class="btn" id="usage-prev" ${previous.length ? "" : "disabled"}>较新记录</button><button class="btn" id="usage-next" ${data.next_cursor ? "" : "disabled"}>较早记录</button></div></section>
    <p class="usage-note">仅统计启用此功能后插件主动发起的巩固、图片描述和音频转写，包含重试；不包含主聊天回复。总 token = 未缓存输入 + 缓存输入 + 输出，缓存不重复累加。未返回 usage 的调用显示“未知”，失败也可能产生未报告的费用。混合媒体无法按图片、音频拆分 token。明细记录调用结果，不代表记忆写入结果；数据不含提示词、媒体或回复正文。</p>
  </div>`;
  $("usage-next").onclick = () => loadUsage(data.next_cursor, [...previous, before]);
  $("usage-prev").onclick = () => loadUsage(previous.at(-1), previous.slice(0, -1));
  const chart = $("content").querySelector(".usage-chart");
  if (chart) {
    const showDay = event => { const day = event.target.closest("[data-day]"); if (day) $("usage-day").textContent = day.dataset.day; };
    for (const type of ["pointerover", "focusin", "click"]) chart.addEventListener(type, showDay);
  }
}

RETRY_LOADERS.usage = () => loadUsage();
