"""
编码（Encoding）：决定什么值得被记住。

私聊：每轮 LLM 响应后，直接用后台小模型判定+抽取（低门槛）。
群聊：两级门控——规则预过滤（零成本）命中后，才调用后台小模型判定+抽取（高门槛），
      避免群聊海量消息导致后台模型调用成本失控。
"""

from __future__ import annotations

import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse

from .llm_helper import call_background_llm, parse_json_object
from .scope import MemoryScope, private_scope_key, resolve_scope
from .storage import MemoryStore

_WAKE_KEYWORDS = ("记住", "别忘了", "记一下")

_ENCODE_PROMPT_TEMPLATE = """你是记忆编码模块，负责判断一段对话是否包含值得长期记住的事实。

对话内容：
{conversation}

请判断这段对话中是否存在值得记住的具体事实（例如：偏好、状态变化、重要事件、承诺、身份信息），
并按以下 JSON 格式输出，不要输出多余文字：

{{
  "worth_recording": true/false,
  "candidates": [
    {{
      "content": "陈述句形式的事实，例如：用户提到下周要去北京出差",
      "tags": "逗号分隔的关键词，例如：出差,北京,工作",
      "importance": 1-5的整数,
      "is_self_statement": true/false,
      "sensitivity_level": "low/medium/high",
      "sensitivity_category": "health/finance/relationship/location/politics/none 之一"
    }}
  ]
}}

- is_self_statement: 该事实是否是说话人在讲述自己的事（而非转述/评价他人）
- 如果没有值得记住的内容，worth_recording 设为 false，candidates 为空数组
"""


def _should_prefilter_pass(
    event: AstrMessageEvent, min_length: int, sample_rate: float
) -> bool:
    """群聊规则预过滤：零成本判断是否值得送去小模型判定"""
    text = (event.message_str or "").strip()
    if event.is_wake_up():
        return True
    if any(kw in text for kw in _WAKE_KEYWORDS):
        return True
    if len(text) < min_length:
        return False
    # 长度达标但未命中显式规则，按采样率随机放行，模拟背景感知
    return sample_rate > 0 and random.random() < sample_rate


async def _encode_and_store(
    context,
    config: dict,
    store: MemoryStore,
    scope: MemoryScope,
    conversation_text: str,
    event: AstrMessageEvent,
) -> None:
    prompt = _ENCODE_PROMPT_TEMPLATE.format(conversation=conversation_text)
    raw = await call_background_llm(context, config, prompt=prompt, event=event)
    parsed = parse_json_object(raw)
    if not isinstance(parsed, dict) or not parsed.get("worth_recording"):
        return

    candidates = parsed.get("candidates") or []
    if not isinstance(candidates, list):
        return

    bridge_enabled = bool(config.get("enable_cross_scope_bridge", False))
    bridge_max_sensitivity = config.get("bridge_max_sensitivity", "low")
    sensitivity_rank = {"low": 0, "medium": 1, "high": 2}

    for item in candidates:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        tags = str(item.get("tags") or "")
        importance = int(item.get("importance") or 3)
        importance = max(1, min(5, importance))
        sensitivity_level = str(item.get("sensitivity_level") or "low")
        sensitivity_category = item.get("sensitivity_category")
        if sensitivity_category == "none":
            sensitivity_category = None

        await store.insert_memory(
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            memory_type="episodic",
            subject=scope.subject,
            content=content,
            tags=tags,
            importance=importance,
            sensitivity_level=sensitivity_level,
            sensitivity_category=sensitivity_category,
        )

        # 群聊 -> 私聊桥接：需要总开关 + 自我陈述 + 已授权 + 敏感度达标
        if (
            scope.scope_type == "group"
            and bridge_enabled
            and item.get("is_self_statement")
            and scope.subject
        ):
            platform = scope.scope_key.split(":", 1)[0]
            consented = await store.is_bridge_enabled(platform, scope.subject)
            level_ok = sensitivity_rank.get(sensitivity_level, 2) <= sensitivity_rank.get(
                bridge_max_sensitivity, 0
            )
            if consented and level_ok:
                target_key = private_scope_key(platform, scope.subject)
                await store.insert_memory(
                    scope_type="private",
                    scope_key=target_key,
                    memory_type="episodic",
                    subject=None,
                    content=content,
                    tags=tags,
                    importance=importance,
                    sensitivity_level=sensitivity_level,
                    sensitivity_category=sensitivity_category,
                    source_type="group_bridge",
                    source_ref=scope.scope_key,
                )
                await store.touch_scope("private", target_key)

    await store.touch_scope(scope.scope_type, scope.scope_key)


async def handle_private_response(
    context, config: dict, store: MemoryStore, event: AstrMessageEvent, resp: LLMResponse
) -> None:
    """私聊场景：LLM 响应后即时编码"""
    if not config.get("enable_private_memory", True):
        return
    scope = resolve_scope(event)
    user_text = (event.message_str or "").strip()
    assistant_text = (resp.completion_text or "").strip()
    if not user_text and not assistant_text:
        return
    conversation_text = f"用户: {user_text}\n助手: {assistant_text}"
    await _encode_and_store(context, config, store, scope, conversation_text, event)


async def handle_group_message(
    context, config: dict, store: MemoryStore, event: AstrMessageEvent
) -> None:
    """群聊场景：两级门控后编码"""
    if not config.get("enable_group_memory", True):
        return
    min_length = int(config.get("group_prefilter_min_length", 10))
    sample_rate = float(config.get("group_prefilter_sample_rate", 0.03))
    if not _should_prefilter_pass(event, min_length, sample_rate):
        return

    scope = resolve_scope(event)
    sender_name = event.get_sender_name() or scope.subject or "未知用户"
    text = (event.message_str or "").strip()
    if not text:
        return
    conversation_text = f"{sender_name}: {text}"
    await _encode_and_store(context, config, store, scope, conversation_text, event)
