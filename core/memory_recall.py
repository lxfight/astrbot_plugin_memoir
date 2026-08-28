"""
召回（Retrieval）：线索依赖检索，而非语义相似度检索。

检索不用向量、也不依赖 FTS5 分词（SQLite 默认 tokenizer 对中文不可控），
改为 LIKE 子串匹配：用当前用户发言的关键线索对记忆的 content/tags 做子串命中。
中文线索用 bigram（二字滑窗）提取，模拟「具体重合线索触发回忆」；
英文/数字按词切分。命中记录按 洞察 > 语义记忆 > 情景记忆 排序注入，
并强化命中记忆（重置衰减强度，模拟「越常被用到的记忆越难忘」）。
"""

from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest

from .scope import resolve_scope
from .storage import MemoryStore

_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "们", "在", "和", "就",
    "不", "也", "都", "这", "那", "吗", "呢", "吧", "啊", "有", "个", "上",
    "a", "the", "is", "are", "am", "to", "of", "and", "in", "it",
}

# CJK 汉字范围（基本区 + 扩展A），用于识别中文片段
_CJK_RE = re.compile(r"[一-鿿]+")
# ASCII 词（字母/数字）
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _extract_terms(text: str, max_terms: int = 12) -> list[str]:
    """提取检索线索：中文 bigram + 英文/数字词，去重、过滤停用词、限数。

    用 bigram 而非整段中文，是因为记忆 content 是另一句不同的话，
    整段子串几乎不会重合，但「北京」「出差」这类二字线索会重合——
    这正是人脑「编码特异性」式的线索触发。
    """
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()

    for cjk in _CJK_RE.findall(text):
        # 对每段中文做二字滑窗
        for i in range(len(cjk) - 1):
            gram = cjk[i : i + 2]
            if gram in seen:
                continue
            seen.add(gram)
            terms.append(gram)
            if len(terms) >= max_terms:
                return terms
        # 单字中文片段也作为线索（整段只有一个汉字时）
        if len(cjk) == 1 and cjk not in seen and cjk not in _STOPWORDS:
            seen.add(cjk)
            terms.append(cjk)

    for word in _ASCII_WORD_RE.findall(text):
        w = word.strip()
        if not w or w.lower() in _STOPWORDS:
            continue
        if len(w) < 2:
            continue
        if w in seen:
            continue
        seen.add(w)
        terms.append(w)
        if len(terms) >= max_terms:
            break
    return terms


def _format_memory_block(memories: list[dict]) -> str:
    lines = ["[长期记忆]"]
    for mem in memories:
        prefix = ""
        if mem.get("memory_type") == "insight":
            prefix = "[洞察] "
        elif mem.get("memory_type") == "semantic":
            prefix = "[认知] "
        subject = mem.get("subject")
        subject_hint = f"（{subject}）" if subject else ""
        lines.append(f"- {prefix}{mem['content']}{subject_hint}")
    return "\n".join(lines)


async def handle_recall(
    context, config: dict, store: MemoryStore, event: AstrMessageEvent, req: ProviderRequest
) -> None:
    """on_llm_request 钩子：检索并注入记忆"""
    scope_type = "private" if event.is_private_chat() else "group"
    if scope_type == "private" and not config.get("enable_private_memory", True):
        return
    if scope_type == "group" and not config.get("enable_group_memory", True):
        return

    scope = resolve_scope(event)
    query_text = req.prompt or event.message_str or ""
    terms = _extract_terms(query_text)
    if not terms:
        return

    top_k = int(config.get("recall_top_k", 5))
    memories = await store.search_memories(scope.scope_type, scope.scope_key, terms, top_k)
    if not memories:
        return

    memory_block = _format_memory_block(memories)
    req.extra_user_content_parts.append(TextPart(text=memory_block).mark_as_temp())

    hit_ids = [mem["id"] for mem in memories]
    await store.reinforce_memories(hit_ids)
