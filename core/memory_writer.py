"""
捕获（Capture）：原始对话轮次零成本落库。

不做任何 LLM 判定与信息压缩——值得记住什么、怎么提炼，交给周期巩固的批量抽取。
原文逐字保留，写入无信息损失，抽取质量也比逐轮孤立判定更高。

- 私聊：on_llm_response 后把「用户 + 助手」完整一轮落库。
- 群聊：被动捕获每条消息落库（此前为控制 LLM 成本的两级门控已无存在必要）；
  机器人自己的回复在 on_llm_response 后补记——被动捕获只覆盖入站消息，
  若不补记 bot 侧，群聊巩固时提取的认知会缺失机器人说过什么的上下文。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Record
from astrbot.api.provider import LLMResponse

from .scope import merge_scope_config, resolve_scope
from .storage import MemoryStore

_MAX_CONTENT_LENGTH = 2000


def _capture_text(event: AstrMessageEvent) -> str:
    """消息链转捕获文本：文本保留，多媒体记占位符。

    占位符防止巩固时信息凭空消失（LLM 至少知道这里发过图/语音）；
    URL 不落库——对检索只有噪声，extract_terms 也会过滤占位符。
    """
    text = (event.message_str or "").strip()
    chain = getattr(event.message_obj, "message", None) or []
    n_image = sum(1 for c in chain if isinstance(c, Image))
    n_record = sum(1 for c in chain if isinstance(c, Record))
    if n_image:
        text += f" [图片x{n_image}]" if n_image > 1 else " [图片]"
    if n_record:
        text += f" [语音x{n_record}]" if n_record > 1 else " [语音]"
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
    await store.insert_raw_turn(
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        content=text[:_MAX_CONTENT_LENGTH],
        speaker_id=scope.subject,
        speaker_name=event.get_sender_name() or None,
    )
