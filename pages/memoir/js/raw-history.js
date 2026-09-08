import { $, beginLoad, RETRY_LOADERS, state } from "./state.js";
import { dayLabel, emptyState, errorPanel, refreshIcons, skeleton } from "./utils.js";
import { bridge } from "./api.js";
import { renderSelection } from "./list.js";
import { renderRawEvent } from "./raw.js";

let active = null;

export function stopRawHistory() {
  if (!active) return;
  active.observer.disconnect();
  active.controller.abort();
  cancelAnimationFrame(active.frame);
  if (state.items === active.rows) state.items = [];
  active = null;
  $("content").classList.remove("raw-history-mode");
}

// Prefix offsets let scroll and anchor lookup avoid scanning all loaded records.
function rebuildOffsets(session) {
  session.offsets = [0];
  session.positions = new Map();
  for (const row of session.rows) {
    session.positions.set(row.id, session.offsets.length - 1);
    session.offsets.push(session.offsets.at(-1) + (session.heights.get(row.id) || 180));
  }
}

function captureAnchor(session) {
  const top = session.scroller.scrollTop - session.start;
  let lo = 0, hi = session.rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (session.offsets[mid + 1] <= top) lo = mid + 1;
    else hi = mid;
  }
  const index = Math.min(lo, session.rows.length - 1);
  return index < 0 ? null : { id: session.rows[index].id, delta: session.offsets[index] - top };
}

function renderWindow(session, anchor = captureAnchor(session), bottom = false) {
  if (active !== session || !session.request.current() || !session.rows.length) return;
  session.start = session.scroller.scrollTop + session.top.getBoundingClientRect().top - session.scroller.getBoundingClientRect().top;
  const index = anchor ? session.positions.get(anchor.id) ?? -1 : -1;
  const atBottom = bottom || session.scroller.scrollHeight - session.scroller.scrollTop - session.scroller.clientHeight < 2;
  const desired = atBottom ? Math.max(0, session.offsets.at(-1) - session.scroller.clientHeight) : index < 0 ? session.scroller.scrollTop - session.start : session.offsets[index] - anchor.delta;
  const bounds = [Math.max(0, desired - 800), desired + session.scroller.clientHeight + 800];
  const range = bounds.map(target => {
    let lo = 0, hi = session.rows.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (session.offsets[mid + 1] < target) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  });
  const first = Math.max(0, Math.min(range[0], session.rows.length - 1));
  const end = Math.min(session.rows.length, Math.max(first + 1, Math.min(range[1] + 1, first + 60)));
  const ids = new Set(session.rows.slice(first, end).map(row => row.id));
  for (const [id, node] of session.nodes) {
    if (!ids.has(id)) {
      session.observer.unobserve(node); node.remove(); session.nodes.delete(id);
    }
  }
  let cursor = session.visible.firstChild;
  let mounted = false;
  for (let i = first; i < end; i++) {
    const row = session.rows[i];
    let node = session.nodes.get(row.id);
    if (!node) {
      mounted = true;
      node = document.createElement("div");
      node.className = "history-row"; node.dataset.eventId = row.id;
      node.innerHTML = renderRawEvent(row, i ? dayLabel(session.rows[i - 1].created_at) : "");
      node.querySelectorAll(".long-text").forEach((el, part) => { el.open = session.expanded.has(`${row.id}:${part}`); });
      session.nodes.set(row.id, node); session.observer.observe(node);
    }
    if (node !== cursor) session.visible.insertBefore(node, cursor);
    cursor = node.nextSibling;
  }
  session.top.style.height = `${session.offsets[first]}px`;
  session.bottom.style.height = `${session.offsets.at(-1) - session.offsets[end]}px`;
  if (mounted) refreshIcons();
  let changed = false;
  for (const [id, node] of session.nodes) {
    const height = node.getBoundingClientRect().height;
    if (Math.abs((session.heights.get(id) || 180) - height) > 0.5) {
      session.heights.set(id, height); changed = true;
    }
  }
  if (changed) {
    rebuildOffsets(session);
    session.top.style.height = `${session.offsets[first]}px`;
    session.bottom.style.height = `${session.offsets.at(-1) - session.offsets[end]}px`;
  }
  session.scroller.scrollTop = atBottom ? session.scroller.scrollHeight : index < 0 ? session.scroller.scrollTop : session.start + session.offsets[index] - anchor.delta;
  if (changed) scheduleRender(session);
}

function scheduleRender(session) {
  if (active !== session || session.frame) return;
  session.frame = requestAnimationFrame(() => { session.frame = 0; renderWindow(session); });
}

async function fetchHistory(session, initial = false) {
  if (active !== session || session.loading || (!initial && !session.hasMore) || !session.request.current()) return;
  session.loading = true; session.failed = false;
  if (!initial) { session.edge.textContent = "正在加载更早消息…"; session.edge.disabled = true; }
  try {
    // The host bridge has no transport-abort API. Disposed generations ignore late responses.
    const result = await bridge.apiGet("browse", { ...session.params, ...(initial ? {} : { before: session.cursor }) });
    if (active !== session || !session.request.current()) return;
    const anchor = initial ? null : captureAnchor(session);
    const known = new Set(session.rows.map(row => row.id));
    const added = result.items.filter(row => !known.has(row.id) && known.add(row.id));
    session.rows = [...added, ...session.rows].sort((a, b) => a.created_at - b.created_at || a.id - b.id);
    session.hasMore = Boolean(result.has_more && result.next_cursor && result.next_cursor !== session.cursor);
    session.cursor = result.next_cursor;
    state.items = session.rows;
    if (!session.rows.length) {
      session.scroller.innerHTML = emptyState("search-x", "没有符合条件的消息，可清除筛选后重试");
      $("result-count").textContent = "没有符合条件的消息"; refreshIcons(); return;
    }
    if (initial) {
      session.scroller.innerHTML = '<div class="chat history-chat"><button class="history-edge" id="raw-history-edge"></button><div class="history-spacer" id="raw-top"></div><div id="raw-visible"></div><div class="history-spacer" id="raw-bottom"></div></div>';
      session.edge = $("raw-history-edge"); session.top = $("raw-top"); session.visible = $("raw-visible"); session.bottom = $("raw-bottom");
      session.edge.addEventListener("click", () => fetchHistory(session), { signal: session.controller.signal });
    } else {
      // Prepending can change the day divider on the previously first event.
      const oldFirst = session.nodes.get(session.firstId);
      if (oldFirst) { session.observer.unobserve(oldFirst); oldFirst.remove(); session.nodes.delete(session.firstId); }
    }
    session.firstId = session.rows[0].id;
    rebuildOffsets(session);
    session.edge.textContent = session.hasMore ? "向上滚动加载更早消息 · 点击加载" : "已到达最早的匹配消息";
    session.edge.disabled = !session.hasMore;
    $("result-count").textContent = `已加载 ${session.rows.length} 个原始事件${session.hasMore ? " · 向上滚动查看历史" : " · 已全部加载"}`;
    renderSelection(); renderWindow(session, anchor, initial);
    // Fill a short first viewport without requiring an impossible scroll gesture.
    if (session.hasMore && session.scroller.scrollHeight <= session.scroller.clientHeight + 1) {
      requestAnimationFrame(() => fetchHistory(session));
    }
  } catch (error) {
    if (active !== session || !session.request.current()) return;
    session.failed = true;
    if (initial) session.scroller.innerHTML = errorPanel("消息", "加载失败，请重试", "raw");
    else { session.edge.textContent = "加载失败 · 点击重试"; session.edge.disabled = false; }
    refreshIcons();
  } finally { session.loading = false; }
}

export function toggleRawSelection() {
  const session = active;
  if (!session?.rows.length) return;
  const anchor = captureAnchor(session);
  session.observer.disconnect();
  session.nodes.forEach(node => node.remove()); session.nodes.clear();
  renderWindow(session, anchor); renderSelection();
}

export async function loadRaw() {
  stopRawHistory();
  const request = beginLoad();
  const params = { ...request.scope, kind: "raw", mode: "cursor", page_size: 40, q: state.q, ...state.filters };
  for (const key of ["since", "until"]) {
    if (!params[key]) continue;
    const date = new Date(`${params[key]}T00:00:00`);
    if (key === "until") date.setDate(date.getDate() + 1);
    params[key] = Math.floor(date.getTime() / 1000);
  }
  const session = { request, params, rows: [], offsets: [0], heights: new Map(), nodes: new Map(), expanded: new Set(), frame: 0, start: 0, scroller: $("content"), controller: new AbortController(), hasMore: true, loading: false, failed: false };
  session.observer = new ResizeObserver(() => scheduleRender(session));
  active = session;
  state.items = []; state.selected.clear(); renderSelection();
  session.scroller.classList.add("raw-history-mode");
  session.scroller.innerHTML = skeleton();
  $("pager").style.display = "none";
  const options = { signal: session.controller.signal };
  session.scroller.addEventListener("scroll", () => {
    if (!session.rows.length) return;
    scheduleRender(session);
    if (session.scroller.scrollTop < 240 && !session.failed) fetchHistory(session);
  }, { ...options, passive: true });
  session.scroller.addEventListener("toggle", event => {
    if (!event.target.matches(".long-text")) return;
    const node = event.target.closest(".history-row");
    const part = [...node.querySelectorAll(".long-text")].indexOf(event.target);
    const key = `${node.dataset.eventId}:${part}`;
    event.target.open ? session.expanded.add(key) : session.expanded.delete(key);
    scheduleRender(session);
  }, { ...options, capture: true });
  window.addEventListener("resize", () => {
    session.heights.clear(); scheduleRender(session);
  }, options);
  await fetchHistory(session, true);
}

RETRY_LOADERS.raw = loadRaw;
