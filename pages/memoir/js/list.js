import { $, beginLoad, state } from "./state.js";
import { esc, emptyState, errorPanel, refreshIcons, renderPager, skeleton } from "./utils.js";
import { bridge, safe } from "./api.js";

export function renderFilters() {
  const raw = state.tab === "raw";
  const fields = [
    ["source", "来源", [["", "全部来源"], ["native", "普通对话"], ["forwarded", "转发引用"]]],
    ...(raw ? [["status", "处理状态", [["", "全部状态"], ["pending", "解析排队"], ["running", "正在解析"], ["complete", "已完成"], ["partial", "部分解析"], ["failed", "处理失败"], ["skipped", "策略跳过"], ["paused", "额度暂停"], ["unsupported", "暂不支持"], ["unextracted", "待巩固"]]]] : [
      ["memory_type", "记忆类型", [["", "全部类型"], ["semantic", "认知"], ["insight", "洞察"]]],
      ["importance", "重要度", [["", "全部重要度"], ...[1,2,3,4,5].map(n => [String(n), `${n} 级`])]],
      ["sort", "排序", [["", "最近更新"], ["importance", "重要度优先"]]],
    ]),
  ];
  $("filters").innerHTML = fields.map(([key, label, options]) => `<label>${label}<select data-filter="${key}">${options.map(([value, text]) => `<option value="${value}" ${String(state.filters[key] || "") === value ? "selected" : ""}>${text}</option>`).join("")}</select></label>`).join("") + (raw ? `<label>实际发送者<input data-filter="sender" placeholder="ID 或昵称" value="${esc(state.filters.sender || "")}"></label><label>开始日期<input type="date" data-filter="since" value="${esc(state.filters.since || "")}"></label><label>结束日期<input type="date" data-filter="until" value="${esc(state.filters.until || "")}"></label>` : "") + `<button class="btn" id="clear-filters">清除筛选</button><button class="btn" id="toggle-selection">${state.selecting ? "退出选择" : "选择记录"}</button><span id="result-count" aria-live="polite"></span>`;
  $("search").value = state.q;
  $("search").placeholder = raw ? "搜索消息正文（包含转发分块）" : "搜索记忆正文或标签";
  $("search").setAttribute("aria-label", $("search").placeholder);
  renderSelection();
}
export function renderSelection() {
  $("selection-bar").hidden = !state.selecting;
  const raw = state.tab === "raw";
  const candidates = raw ? state.items.slice(-100) : state.items;
  $("selection-bar").innerHTML = `<label><input type="checkbox" id="select-page" ${candidates.length && candidates.every(row => state.selected.has(row.id)) ? "checked" : ""}>${raw ? "选择已加载记录（最近最多 100 条）" : "选择当前页"}</label><span>已选 ${state.selected.size} 条，${raw ? "仅限已加载记录" : "仅限当前页"}</span><button class="btn danger" id="delete-selection" ${state.selected.size ? "" : "disabled"}>删除所选</button>`;
}

export async function loadRecords(render) {
  const request = beginLoad();
  const kind = state.tab;
  const params = { ...request.scope, kind, q: state.q, ...state.filters, page: state.page, page_size: state.pageSize };
  if (kind === "raw") {
    for (const key of ["since", "until"]) {
      if (!params[key]) continue;
      const date = new Date(`${params[key]}T00:00:00`);
      if (key === "until") date.setDate(date.getDate() + 1);
      params[key] = Math.floor(date.getTime() / 1000);
    }
  }
  const scroll = $("content").scrollTop;
  $("content").innerHTML = skeleton();
  const result = await safe("加载记录", () => bridge.apiGet("browse", params), { silent: true });
  if (!request.current()) return;
  if (!result) {
    state.items = []; state.selected.clear(); renderSelection();
    $("content").innerHTML = errorPanel("记录", "请求失败，请重试", kind);
    $("pager").style.display = "none"; refreshIcons(); return;
  }
  state.page = result.page || state.page;
  state.items = result.items;
  const ids = new Set(result.items.map(row => row.id));
  state.selected = new Set([...state.selected].filter(id => ids.has(id)));
  $("content").innerHTML = result.items.length ? render(result.items) : emptyState("search-x", "没有符合条件的记录，可清除筛选后重试");
  $("content").scrollTop = scroll;
  $("result-count").textContent = `共 ${result.total} ${kind === "raw" ? "个原始事件" : "条记忆"}${state.q ? ` · 搜索「${state.q}」` : ""}`;
  renderPager(result.total, result.page_size || state.pageSize);
  renderSelection(); refreshIcons();
}
