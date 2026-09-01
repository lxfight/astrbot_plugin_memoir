export const $ = (id) => document.getElementById(id);

export const TYPE_NAMES = { semantic: "认知", insight: "洞察", raw: "对话", episodic: "往事" };
export const TYPE_ICONS = { semantic: "lightbulb", insight: "sparkles", episodic: "history", raw: "message-circle" };

export const state = {
  scopes: [],
  scope: null,
  tab: "memories",
  page: 1,
  pageSize: 20,
  q: "",
  scopeFilter: "",
  globalConfig: {},
  providers: [],
  scopeOverride: {},
};

export const RETRY_LOADERS = {};
