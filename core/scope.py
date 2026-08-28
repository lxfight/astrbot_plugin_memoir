"""
会话归属解析：决定一条消息应该落到哪个记忆 scope。

私聊：scope_key = platform:sender_id，记忆天然属于这个人。
群聊：scope_key = platform:group_id，默认整群共享一个记忆池；
      具体发言人记录在 subject 字段，供追溯，但不影响检索范围。
"""

from __future__ import annotations

from dataclasses import dataclass

from astrbot.api.event import AstrMessageEvent


@dataclass(frozen=True)
class MemoryScope:
    scope_type: str  # 'private' | 'group'
    scope_key: str
    subject: str | None  # 群聊场景下的具体发言人 sender_id，私聊为 None


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
