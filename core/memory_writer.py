"""
Capture conversation text and descriptions of supported incoming media.

Text capture does not call an LLM. Supported images and audio are described
before storage; decisions about lasting memories remain in consolidation.

- 私聊：on_llm_response 后把「用户 + 助手」完整一轮落库。
- 群聊：被动捕获每条消息落库（此前为控制 LLM 成本的两级门控已无存在必要）；
  机器人自己的回复在 on_llm_response 后补记——被动捕获只覆盖入站消息，
  若不补记 bot 侧，群聊巩固时提取的认知会缺失机器人说过什么的上下文。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Record, Video
from astrbot.api.provider import LLMResponse

from .llm_helper import describe_multimedia
from .scope import merge_scope_config, resolve_scope
from .storage import MemoryStore

_MAX_CONTENT_LENGTH = 2000


def _capture_text(event: AstrMessageEvent) -> str:
    """Preserve message text with placeholders for attached media.

    Args:
        event: Incoming message event.

    Returns:
        Text with media counts, without attachment URLs or binary data.
    """
    text = (event.message_str or "").strip()
    chain = getattr(event.message_obj, "message", None) or []
    for component, label in (
        (Image, "图片"),
        (Record, "语音"),
        (Video, "视频"),
        (File, "文件"),
    ):
        count = sum(1 for part in chain if isinstance(part, component))
        if count:
            text += f" [{label}x{count}]" if count > 1 else f" [{label}]"
    return text.strip()


async def handle_private_response(
    context,
    config: dict,
    store: MemoryStore,
    event: AstrMessageEvent,
    resp: LLMResponse,
) -> None:
    """私聊场景：LLM 响应后把完整一轮对话落库（全局开关 + 会话覆盖叠加判断）"""
    if not config.get("enable_private_memory", True):
        return
    scope = resolve_scope(event)
    config = merge_scope_config(
        config,
        await store.get_scope_config(scope.scope_type, scope.scope_key),
        scope.scope_type,
    )
    if not config.get("scope_enabled", True):
        return
    user_text = _capture_text(event)
    description = await describe_multimedia(context, config, event)
    if description:
        user_text = f"{user_text[:1000]} [多媒体解析] {description}"
    assistant_text = (resp.completion_text or "").strip()
    if not user_text and not assistant_text:
        return
    content = f"用户: {user_text} / 助手: {assistant_text}"
    await store.insert_raw_turn(
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        content=content[:_MAX_CONTENT_LENGTH],
    )


async def handle_group_response(
    context,
    config: dict,
    store: MemoryStore,
    event: AstrMessageEvent,
    resp: LLMResponse,
) -> None:
    """群聊场景：机器人自己的回复落库（用户侧消息已由被动捕获落库）。

    Args:
        context: AstrBot 上下文（与私聊落库保持一致签名，当前未使用）。
        config: 全局配置字典，调用侧会先合并会话覆盖。
        store: 记忆存储实例。
        event: 触发本次 LLM 响应的群聊事件。
        resp: LLM 响应，取 completion_text 作为 bot 侧原文。
    """
    if not config.get("enable_group_memory", True):
        return
    if not event.get_group_id():
        # 适配器未提供群号时无法归属 scope，跳过以免所有群混入同一记忆池
        return
    assistant_text = (resp.completion_text or "").strip()
    if not assistant_text:
        return
    scope = resolve_scope(event)
    config = merge_scope_config(
        config,
        await store.get_scope_config(scope.scope_type, scope.scope_key),
        scope.scope_type,
    )
    if not config.get("scope_enabled", True):
        return
    await store.insert_raw_turn(
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        content=f"助手: {assistant_text}"[:_MAX_CONTENT_LENGTH],
    )


async def handle_group_message(
    context, config: dict, store: MemoryStore, event: AstrMessageEvent
) -> None:
    """群聊场景：被动捕获的消息原文落库（指令、忽略名单用户与关键词命中的消息除外）"""
    if not config.get("enable_group_memory", True):
        return
    if not event.get_group_id():
        # 适配器未提供群号时无法归属 scope，跳过以免所有群混入同一记忆池
        return
    text = _capture_text(event)
    if not text or text.startswith("/"):
        return
    scope = resolve_scope(event)
    config = merge_scope_config(
        config,
        await store.get_scope_config(scope.scope_type, scope.scope_key),
        scope.scope_type,
    )
    if not config.get("scope_enabled", True):
        return
    # 隐私过滤：名单用户与关键词命中的消息不落库（捕获侧丢弃，召回自然不可见）
    ignored_users = {
        str(u).strip()
        for u in config.get("group_capture_ignored_users") or []
        if str(u).strip()
    }
    if scope.subject and scope.subject in ignored_users:
        return
    ignored_keywords = [
        str(k).strip()
        for k in config.get("group_capture_ignored_keywords") or []
        if str(k).strip()
    ]
    if any(k in text for k in ignored_keywords):
        return
    description = await describe_multimedia(context, config, event)
    if description:
        # Apply the same privacy filter to recognized speech and image text.
        if any(k in description for k in ignored_keywords):
            return
        text = f"{text[:1000]} [多媒体解析] {description}"
    await store.insert_raw_turn(
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        content=text[:_MAX_CONTENT_LENGTH],
        speaker_id=scope.subject,
        speaker_name=event.get_sender_name() or None,
    )
