"""
WebUI 后端接口：供插件 Pages（pages/memoir/）通过 bridge 调用。

注意：AstrBot 核心的 registered_web_apis 没有注销机制，插件 terminate 后
注册的 handler 会残留。因此本类持有插件实例的弱引用而非 store/config 的
强引用——插件被禁用/卸载后实例可被正常回收，残留的 HTTP 调用会得到
明确的错误响应，而不是触达已关闭的数据库连接。
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

from astrbot.api.web import error_response, json_response, request

from .memory_recall import extract_terms
from .storage import MemoryStore

if TYPE_CHECKING:
    from main import MemoirPlugin

_PAGE_SIZE = 20
_SEARCH_LIMIT = 50

# ---- 配置白名单与默认值（WebUI 配置编辑的后端校验依据）----
GLOBAL_CONFIG_KEYS = (
    "enable_private_memory",
    "enable_group_memory",
    "background_llm_provider",
    "recall_top_k",
    "recall_max_chars",
    "consolidation_related_top_k",
    "recall_core_top_k",
    "recall_recent_turns",
    "consolidation_scan_interval_minutes",
    "consolidation_count_threshold_private",
    "consolidation_count_threshold_group",
    "consolidation_idle_hours",
    "raw_retention_days",
    "decay_rate_semantic",
    "decay_rate_insight",
    "enable_cross_scope_bridge",
    "bridge_max_sensitivity",
    "group_capture_ignored_users",
    "group_capture_ignored_keywords",
)
GLOBAL_DEFAULTS = {
    "enable_private_memory": True,
    "enable_group_memory": True,
    "background_llm_provider": "",
    "recall_top_k": 5,
    "recall_max_chars": 6000,
    "consolidation_related_top_k": 20,
    "recall_core_top_k": 3,
    "recall_recent_turns": 5,
    "consolidation_scan_interval_minutes": 30,
    "consolidation_count_threshold_private": 20,
    "consolidation_count_threshold_group": 50,
    "consolidation_idle_hours": 12,
    "raw_retention_days": 14,
    "decay_rate_semantic": 0.98,
    "decay_rate_insight": 0.995,
    "enable_cross_scope_bridge": False,
    "bridge_max_sensitivity": "low",
    "group_capture_ignored_users": [],
    "group_capture_ignored_keywords": [],
}
GLOBAL_SELECTS = {"bridge_max_sensitivity": ("low", "medium", "high")}

# 会话级可覆盖键：null/缺失 = 继承全局
SCOPE_CONFIG_KEYS = (
    "enabled",
    "recall_top_k",
    "recall_max_chars",
    "recall_core_top_k",
    "recall_recent_turns",
    "consolidation_count_threshold",
    "consolidation_idle_hours",
    "bridge_enabled",
    "bridge_max_sensitivity",
)
SCOPE_SELECTS = {"bridge_max_sensitivity": ("low", "medium", "high")}

_INT_KEYS = {
    "recall_top_k",
    "recall_max_chars",
    "consolidation_related_top_k",
    "recall_core_top_k",
    "recall_recent_turns",
    "consolidation_scan_interval_minutes",
    "consolidation_count_threshold_private",
    "consolidation_count_threshold_group",
    "consolidation_idle_hours",
    "raw_retention_days",
    "consolidation_count_threshold",
}
_FLOAT_KEYS = {"decay_rate_semantic", "decay_rate_insight"}
_LIST_KEYS = {"group_capture_ignored_users", "group_capture_ignored_keywords"}
_BOOL_KEYS = {
    "enable_private_memory",
    "enable_group_memory",
    "enable_cross_scope_bridge",
    "enabled",
    "bridge_enabled",
}


def _validate_config_payload(
    payload: dict,
    keys: tuple[str, ...],
    selects: dict[str, tuple[str, ...]],
) -> dict | None:
    """按白名单清洗配置 payload，类型不合法返回 None（整体拒绝）。值为 None 表示继承全局，跳过。"""
    cleaned: dict = {}
    for key, value in payload.items():
        if key not in keys:
            continue
        if value is None:
            continue
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                return None
            cleaned[key] = value
        elif key in _INT_KEYS:
            try:
                cleaned[key] = max(0, int(value))
                if key == "recall_max_chars":
                    cleaned[key] = min(20000, max(512, cleaned[key]))
                elif key.startswith("recall_") or key == "consolidation_related_top_k":
                    cleaned[key] = min(100, cleaned[key])
            except (TypeError, ValueError):
                return None
        elif key in _FLOAT_KEYS:
            try:
                cleaned[key] = min(1.0, max(0.0, float(value)))
            except (TypeError, ValueError):
                return None
        elif key in _LIST_KEYS:
            # 接受列表或逗号分隔字符串，统一归一化为去空白的字符串列表
            items = value.split(",") if isinstance(value, str) else value
            if not isinstance(items, list):
                return None
            cleaned[key] = [str(item).strip() for item in items if str(item).strip()]
        elif key in selects:
            if value not in selects[key]:
                return None
            cleaned[key] = value
        else:
            cleaned[key] = str(value).strip()
    return cleaned


class WebApi:
    """插件 WebUI 接口集合，handler 由 main.py 注册到 context"""

    def __init__(self, plugin: MemoirPlugin):
        self._plugin_ref = weakref.ref(plugin)

    def _ctx(self) -> tuple[MemoryStore, dict[str, Any]] | None:
        """返回 (store, config)；插件已终止/卸载时返回 None"""
        plugin = self._plugin_ref()
        if plugin is None or plugin._terminating or not plugin._initialized:
            return None
        return plugin.store, plugin.config

    async def processing_status(self):
        """Return scoped queue counts, backlog age and retryable failures."""
        ctx, scope = self._ctx(), self._require_scope()
        if ctx is None or scope is None:
            return error_response("plugin or scope unavailable")
        return json_response(await ctx[0].get_processing_status(*scope))

    async def retry_processing(self):
        """Requeue a failed job while respecting current memory switches."""
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin unavailable")
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid payload")
        scope = self._scope_from_payload(payload)
        try:
            work_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("invalid work id")
        if scope is None:
            return error_response("scope required")
        store, config = ctx
        async with store.transaction():
            override = await store.get_scope_config(*scope)
            if (
                not config.get(f"enable_{scope[0]}_memory", True)
                or override.get("enabled") is False
            ):
                return error_response("memory is disabled for this conversation")
            if not await store.retry_work(work_id, *scope):
                return error_response(
                    "source expired, queue full, or task is not retryable"
                )
        plugin = self._plugin_ref()
        plugin.event_handler.media.wakeup.set()
        plugin.event_handler.scheduler.wakeup.set()
        return json_response({"queued": work_id})

    async def memory_sources(self):
        """Return source text only within the selected memory's scope."""
        ctx, scope = self._ctx(), self._require_scope()
        if ctx is None or scope is None:
            return error_response("plugin or scope unavailable")
        memory_id = request.query.get("id", 0, type=int)
        result = await ctx[0].get_memory_sources(memory_id, *scope)
        return (
            json_response(result)
            if result is not None
            else error_response("memory not found")
        )

    async def overview(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, config = ctx
        scopes = await store.list_scopes()
        return json_response(
            {
                "scopes": scopes,
                "totals": {
                    "scopes": len(scopes),
                    "memories": sum(s["memory_count"] for s in scopes),
                    "raw_turns": sum(s["raw_count"] for s in scopes),
                    "pending": sum(s["pending_count"] for s in scopes),
                },
                "config": {
                    "consolidation_scan_interval_minutes": config.get(
                        "consolidation_scan_interval_minutes", 30
                    ),
                    "count_threshold_private": config.get(
                        "consolidation_count_threshold_private", 20
                    ),
                    "count_threshold_group": config.get(
                        "consolidation_count_threshold_group", 50
                    ),
                    "raw_retention_days": config.get("raw_retention_days", 14),
                    "enable_cross_scope_bridge": config.get(
                        "enable_cross_scope_bridge", False
                    ),
                },
            }
        )

    async def memories(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        scope = self._require_scope()
        if scope is None:
            return error_response("scope_type and scope_key are required")
        q = (request.query.get("q") or "").strip()
        if q:
            items = await store.search_memories(
                scope[0], scope[1], extract_terms(q), top_k=_SEARCH_LIMIT
            )
            return json_response({"items": items, "search": True, "total": len(items)})

        page = max(1, request.query.get("page", 1, type=int))
        page_size = max(
            1, min(100, request.query.get("page_size", _PAGE_SIZE, type=int))
        )
        items = await store.get_scope_memories(
            scope[0],
            scope[1],
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        total = await store.count_scope_memories(scope[0], scope[1])
        return json_response(
            {
                "items": items,
                "search": False,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    async def delete_memory(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        payload = await request.json(default={})
        scope = self._scope_from_payload(payload)
        if scope is None:
            return error_response("scope_type and scope_key are required")
        try:
            memory_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("id must be an integer")
        deleted = await store.delete_memory_in_scope(memory_id, scope[0], scope[1])
        if not deleted:
            return error_response(f"memory #{memory_id} not found in scope")
        return json_response({"deleted": memory_id})

    async def raw_turns(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        scope = self._require_scope()
        if scope is None:
            return error_response("scope_type and scope_key are required")
        page = max(1, request.query.get("page", 1, type=int))
        page_size = max(
            1, min(100, request.query.get("page_size", _PAGE_SIZE, type=int))
        )
        items, total = await store.get_raw_turns(
            scope[0],
            scope[1],
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return json_response(
            {"items": items, "total": total, "page": page, "page_size": page_size}
        )

    async def delete_raw_turn(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        payload = await request.json(default={})
        scope = self._scope_from_payload(payload)
        if scope is None:
            return error_response("scope_type and scope_key are required")
        try:
            turn_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return error_response("id must be an integer")
        deleted = await store.delete_raw_turn_in_scope(turn_id, scope[0], scope[1])
        if not deleted:
            return error_response(f"raw turn #{turn_id} not found in scope")
        return json_response({"deleted": turn_id})

    async def clear_scope(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        payload = await request.json(default={})
        scope = self._scope_from_payload(payload)
        if scope is None:
            return error_response("scope_type and scope_key are required")
        deleted = await store.delete_scope_memories(scope[0], scope[1])
        return json_response({"deleted": deleted})

    async def consents(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        return json_response({"items": await store.list_bridge_consents()})

    async def toggle_consent(self):
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        payload = await request.json(default={})
        platform = str(payload.get("platform") or "").strip()
        sender_id = str(payload.get("sender_id") or "").strip()
        if not platform or not sender_id:
            return error_response("platform and sender_id are required")
        enabled = bool(payload.get("enabled"))
        await store.set_bridge_enabled(platform, sender_id, enabled)
        return json_response(
            {"platform": platform, "sender_id": sender_id, "enabled": enabled}
        )

    async def get_global_config(self):
        """全局默认配置（白名单键）+ 可用的 chat provider 列表"""
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        _, config = ctx
        providers: list[str] = []
        plugin = self._plugin_ref()
        try:
            pm = getattr(plugin.context, "provider_manager", None)
            if pm is not None:
                from astrbot.core.provider.provider import Provider

                providers = sorted(
                    str(pid)
                    for pid, provider in getattr(pm, "inst_map", {}).items()
                    if isinstance(provider, Provider)
                )
        except Exception:
            providers = []
        return json_response(
            {
                "config": {
                    k: config.get(k, GLOBAL_DEFAULTS.get(k)) for k in GLOBAL_CONFIG_KEYS
                },
                "providers": providers,
            }
        )

    async def update_global_config(self):
        """更新全局配置（白名单键，类型校验），持久化到插件配置文件"""
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, config = ctx
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid config payload")
        updates = _validate_config_payload(payload, GLOBAL_CONFIG_KEYS, GLOBAL_SELECTS)
        if updates is None:
            return error_response("invalid config payload")
        if updates:
            async with store.transaction():
                previous = dict(config)
                try:
                    config.update(updates)
                    save_config = getattr(config, "save_config", None)
                    if callable(save_config):
                        save_config()
                except Exception:
                    config.clear()
                    config.update(previous)
                    raise
                store.config_revision += 1
        return json_response({"updated": sorted(updates)})

    async def get_scope_config(self):
        """读取会话级配置覆盖（空对象 = 完全继承全局）"""
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        scope = self._require_scope()
        if scope is None:
            return error_response("scope_type and scope_key are required")
        override = await store.get_scope_config(scope[0], scope[1])
        return json_response({"override": override})

    async def update_scope_config(self):
        """更新会话配置覆盖；payload 的 override 为空对象时清除覆盖（完全继承全局）"""
        ctx = self._ctx()
        if ctx is None:
            return error_response("plugin is not available")
        store, _ = ctx
        payload = await request.json(default={})
        scope = self._scope_from_payload(payload)
        if scope is None:
            return error_response("scope_type and scope_key are required")
        override = payload.get("override")
        if override is None:
            override = {}
        if not isinstance(override, dict):
            return error_response("override must be an object")
        cleaned = _validate_config_payload(override, SCOPE_CONFIG_KEYS, SCOPE_SELECTS)
        if cleaned is None:
            return error_response("invalid override payload")
        await store.set_scope_config(scope[0], scope[1], cleaned)
        return json_response({"override": cleaned})

    @staticmethod
    def _require_scope() -> tuple[str, str] | None:
        scope_type = request.query.get("scope_type") or ""
        scope_key = request.query.get("scope_key") or ""
        if scope_type not in {"private", "group"} or not scope_key:
            return None
        return scope_type, scope_key

    @staticmethod
    def _scope_from_payload(payload: dict) -> tuple[str, str] | None:
        scope_type = str(payload.get("scope_type") or "").strip()
        scope_key = str(payload.get("scope_key") or "").strip()
        if scope_type not in {"private", "group"} or not scope_key:
            return None
        return scope_type, scope_key
