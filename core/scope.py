"""
会话归属解析：决定一条消息应该落到哪个记忆 scope。

私聊：scope_key = platform:sender_id，记忆天然属于这个人。
群聊：scope_key = platform:group_id，默认整群共享一个记忆池；
      具体发言人记录在 subject 字段，供追溯，但不影响检索范围。

会话级配置覆盖（scope_configs 表）在这里合并进全局配置：
钩子拿到合并后的 dict，无需关心配置来自全局还是会话覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

from astrbot.api.event import AstrMessageEvent

# 会话可覆盖、且会被合并进钩子配置字典的键。
# 「enabled」「bridge_enabled」映射为新键，与全局开关叠加判断。
_DIRECT_KEYS = (
    "recall_top_k",
    "recall_core_top_k",
    "recall_recent_turns",
    "recall_max_chars",
)


@dataclass(frozen=True)
class MemoryScope:
    scope_type: str  # 'private' | 'group'
    scope_key: str
    subject: str | None  # 群聊场景下的具体发言人 sender_id，私聊为 None


def merge_scope_config(base: dict, override: dict | None, scope_type: str) -> dict:
    """把会话级配置覆盖合并进全局配置，返回钩子可直接使用的配置字典。

    覆盖规则（未覆盖的字段保持全局值）：
    - enabled -> scope_enabled（本会话记忆总开关，与全局类型开关叠加）
    - recall_top_k / recall_core_top_k / recall_recent_turns -> 同名键
    - consolidation_count_threshold -> consolidation_count_threshold_{private|group}
    - consolidation_idle_hours -> 同名键
    - bridge_enabled -> scope_bridge_enabled（群聊自我陈述桥接的会话级开关）
    - bridge_max_sensitivity -> 同名键
    """
    cfg = dict(base)
    if not override:
        return cfg
    if override.get("enabled") is not None:
        cfg["scope_enabled"] = bool(override["enabled"])
    for key in _DIRECT_KEYS:
        if override.get(key) is not None:
            try:
                cfg[key] = int(override[key])
            except (TypeError, ValueError):
                continue
    if override.get("consolidation_count_threshold") is not None:
        suffix = "private" if scope_type == "private" else "group"
        try:
            cfg[f"consolidation_count_threshold_{suffix}"] = int(
                override["consolidation_count_threshold"]
            )
        except (TypeError, ValueError):
            pass
    if override.get("consolidation_idle_hours") is not None:
        try:
            cfg["consolidation_idle_hours"] = int(override["consolidation_idle_hours"])
        except (TypeError, ValueError):
            pass
    if override.get("bridge_enabled") is not None:
        cfg["scope_bridge_enabled"] = bool(override["bridge_enabled"])
    if override.get("bridge_max_sensitivity"):
        cfg["bridge_max_sensitivity"] = str(override["bridge_max_sensitivity"])
    return cfg


def resolve_scope(event: AstrMessageEvent) -> MemoryScope:
    platform = event.get_platform_name()
    if event.is_private_chat():
        sender_id = event.get_sender_id()
        return MemoryScope(
            scope_type="private",
            scope_key=f"{platform}:{sender_id}",
            subject=None,
        )
    group_id = event.get_group_id()
    return MemoryScope(
        scope_type="group",
        scope_key=f"{platform}:{group_id}",
        subject=event.get_sender_id(),
    )


def private_scope_key(platform: str, sender_id: str) -> str:
    """群聊桥接时，构造目标用户的私聊 scope_key"""
    return f"{platform}:{sender_id}"
