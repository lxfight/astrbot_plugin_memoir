"""
后台小模型调用封装。

批量抽取（提炼/洞察/自我陈述桥接）等非实时环节统一走这里，
使用户可以配置一个比主对话模型更便宜的模型来处理这些任务。
"""

from __future__ import annotations

import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 后台小模型未配置时的回退告警只提示一次，避免每轮巩固重复刷日志
_FALLBACK_WARNED = False


async def call_background_llm(
    context,
    config: dict,
    *,
    prompt: str,
    system_prompt: str = "",
    event: AstrMessageEvent | None = None,
) -> str | None:
    """调用后台小模型，返回纯文本结果；失败返回 None。

    未配置 background_llm_provider 时，回退到当前会话正在使用的对话模型。
    """
    global _FALLBACK_WARNED

    provider_id = (config or {}).get("background_llm_provider") or ""
    try:
        if provider_id:
            resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        else:
            if not _FALLBACK_WARNED:
                logger.warning(
                    "[Memoir] 未配置后台小模型（background_llm_provider），"
                    "记忆巩固将使用当前对话模型，批量抽取可能产生额外 token 成本"
                )
                _FALLBACK_WARNED = True
            umo = event.unified_msg_origin if event else None
            provider = await context.get_using_provider_async(umo=umo)
            if provider is None:
                logger.warning("[Memoir] 未找到可用的后台/对话模型，跳过本次处理")
                return None
            resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
        return resp.completion_text
    except Exception as exc:
        logger.warning(f"[Memoir] 后台模型调用失败: {exc}")
        return None


def parse_json_object(text: str | None) -> dict | list | None:
    """从模型输出中尽量提取出 JSON 对象/数组，容错处理代码块包裹等情况"""
    if not text:
        return None
    text = text.strip()
    match = _JSON_BLOCK_RE.search(text)
    candidate = match.group(1).strip() if match else text
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        pass
    # 尝试截取首个 { 或 [ 到最后一个匹配符号之间的内容
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                continue
    logger.debug(f"[Memoir] 无法解析模型输出为 JSON: {text[:200]}")
    return None
