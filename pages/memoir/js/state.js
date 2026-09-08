export const $ = (id) => document.getElementById(id);

export const TYPE_NAMES = { semantic: "认知", insight: "洞察", raw: "对话", episodic: "往事" };
export const TYPE_ICONS = { semantic: "lightbulb", insight: "sparkles", episodic: "history", raw: "message-circle" };

export const state = {
  loadVersion: 0,
  overviewVersion: 0,
  scopes: [],
  scope: null,
  tab: "memories",
  page: 1,
  pageSize: 20,
  q: "",
  filters: {},
  items: [],
  selected: new Set(),
  selecting: false,
  busy: false,
  views: new Map(),
  scopeFilter: "",
  globalConfig: {},
  providers: [],
  scopeOverride: {},
};

export const RETRY_LOADERS = {};

// All content loaders share a generation so late responses cannot replace a new view.
export function beginLoad() {
  const version = ++state.loadVersion;
  const scope = { ...state.scope };
  return { scope, current: () => version === state.loadVersion };
}

export const sameScope = (a, b) => Boolean(a && b && a.scope_type === b.scope_type && a.scope_key === b.scope_key);
