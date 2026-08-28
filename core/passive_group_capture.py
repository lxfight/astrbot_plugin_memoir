"""
被动群消息捕获过滤器。

AstrBot 没有提供"监听所有群消息但不唤醒机器人"的标准接口：
普通的 @filter.event_message_type 监听器一旦 filter 通过，WakingCheckStage 就会把
is_wake 置为 True，导致每条群消息都触发机器人回复/LLM 调用。

这里的做法是借助 custom_filter：filter() 在唤醒检查阶段被调用，可以执行捕获副作用，
但返回 False 让该 handler 不进入激活列表，从而不唤醒机器人——模拟
"身处嘈杂房间时背景感知仍在运作，但不会对每句话都回应"。
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter

_ACTIVE_PLUGIN_REF: weakref.ReferenceType[Any] | None = None


def set_active_plugin(plugin: Any) -> None:
    """由 main.py 在初始化时调用，登记插件实例供 filter 副作用使用"""
    global _ACTIVE_PLUGIN_REF
    _ACTIVE_PLUGIN_REF = weakref.ref(plugin) if plugin is not None else None


def _get_active_plugin() -> Any:
    if _ACTIVE_PLUGIN_REF is None:
        return None
    return _ACTIVE_PLUGIN_REF()


class PassiveGroupCaptureFilter(CustomFilter):
    """捕获所有群消息用于编码，但不唤醒机器人回复"""

    def __init__(self, raise_error: bool = True, **kwargs) -> None:
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        # 非 @机器人 或非唤醒前缀的普通群消息也要捕获；
        # 但这条 handler 不该被激活去回复，始终返回 False。
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        plugin = _get_active_plugin()
        if plugin is None:
            return False
        try:
            asyncio.create_task(plugin._dispatch_group_capture(event))
        except RuntimeError:
            # 无运行中的事件循环（极端情况），忽略本次捕获
            pass
        return False
