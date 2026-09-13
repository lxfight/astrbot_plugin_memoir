"""Capture groups passively, deferring direct voice capture until native STT.

Most messages are captured by a background task without activating a handler.
With native STT enabled, voice activates the capture-only handler so it sees
post-preprocessing text. The handler never sets is_at_or_wake_command or calls
an agent, and ProcessStage does not request a reply for ordinary group speech.
"""

from __future__ import annotations

import weakref
from typing import Any

from astrbot.api.message_components import Record
from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter

_ACTIVE_PLUGIN_REF: weakref.ReferenceType[Any] | None = None


def set_active_plugin(plugin: Any) -> None:
    """由 main.py 在初始化时调用，登记插件实例供 filter 副作用使用"""
    global _ACTIVE_PLUGIN_REF
    _ACTIVE_PLUGIN_REF = weakref.ref(plugin) if plugin is not None else None


def clear_active_plugin(plugin: Any) -> None:
    """插件终止时调用；仅当弱引用仍指向该实例时清除，避免影响重载后的新实例"""
    global _ACTIVE_PLUGIN_REF
    if _ACTIVE_PLUGIN_REF is not None and _ACTIVE_PLUGIN_REF() is plugin:
        _ACTIVE_PLUGIN_REF = None


def _get_active_plugin() -> Any:
    if _ACTIVE_PLUGIN_REF is None:
        return None
    return _ACTIVE_PLUGIN_REF()


class PassiveGroupCaptureFilter(CustomFilter):
    """捕获所有群消息用于落库，但不唤醒机器人回复"""

    def __init__(self, raise_error: bool = True, **kwargs) -> None:
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        # Capture ordinary group messages without requesting a main-agent reply.
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        plugin = _get_active_plugin()
        if plugin is None:
            return False
        stt_settings = cfg.get("provider_stt_settings")
        if (
            plugin.config.get("auto_native_compatibility", True)
            and plugin.config.get("enable_group_memory", True)
            and isinstance(stt_settings, dict)
            and stt_settings.get("enable", False)
            and any(isinstance(part, Record) for part in event.get_messages())
        ):
            # A normal handler runs after native preprocessing. It never sets
            # is_at_or_wake_command, calls an LLM or returns a reply.
            from .scope import resolve_scope

            scope = resolve_scope(event)
            event.set_extra("memoir_native_stt", True)
            event.set_extra(
                "memoir_capture_generation",
                plugin.store.generations.get((scope.scope_type, scope.scope_key), 0),
            )
            return True
        # 任务由插件创建并跟踪，terminate 时统一取消
        plugin.submit_group_capture(event)
        return False
