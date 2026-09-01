"""
召回（Retrieval）：线索依赖检索，而非语义相似度检索。

检索不用向量、也不依赖 FTS5 分词（SQLite 默认 tokenizer 对中文不可控），
改为 LIKE 子串匹配：用当前用户发言的关键线索（中文 bigram + 英文词）对
结构化记忆和原始对话轮次做子串命中，模拟「具体重合线索触发回忆」。

三层召回注入：
- 常驻层：importance×strength 最高的语义记忆/洞察固定注入，模拟「稳定认知」；
- 线索层：命中的结构化记忆按相关性排序（tags 命中加权）+ 命中的原始轮次
  （原文可引用，比转述更贴近真人回忆），命中记忆得到强化（重置衰减强度）；
- 近因层：最近的原始轮次，仅群聊注入，补上群聊没有会话历史的短板。
"""

from __future__ import annotations

import re
import time

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest

from .scope import merge_scope_config, resolve_scope
from .storage import MemoryStore

_STOPWORDS = {
    "的",
    "了",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
    "们",
    "在",
    "和",
    "就",
    "不",
    "也",
    "都",
    "这",
    "那",
    "吗",
    "呢",
    "吧",
    "啊",
    "有",
    "个",
    "上",
    "a",
    "the",
    "is",
    "are",
    "am",
    "to",
    "of",
    "and",
    "in",
    "it",
}

# CJK 汉字范围（基本区 + 扩展A），用于识别中文片段
_CJK_RE = re.compile(r"[一-鿿]+")
# ASCII 词（字母/数字）
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")
# URL 与 [图片]/[语音] 等占位符：参与检索只会产生噪声线索
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_PLACEHOLDER_RE = re.compile(r"\[[^\]]*\]")

# 高频功能词 bigram：作为检索线索只有噪声，还会挤占有限的线索名额。
# 只收纯功能词组合，避免误伤「上海」这类含停用字的实词；
# 两字都是单字停用词的组合（"我的""这个"等）由通用规则过滤。
_CJK_BIGRAM_STOPWORDS = {
    "还是",
    "但是",
    "可是",
    "只是",
    "或是",
    "或者",
    "而且",
    "并且",
    "所以",
    "因为",
    "因此",
    "如果",
    "虽然",
    "然后",
    "于是",
    "不过",
    "什么",
    "怎么",
    "我们",
    "你们",
    "大家",
    "自己",
    "别人",
    "一个",
    "一下",
    "一些",
    "一样",
    "一起",
    "一直",
    "没有",
    "不能",
    "不会",
    "不要",
    "不用",
    "可以",
    "应该",
    "需要",
    "可能",
    "已经",
    "这样",
    "那样",
    "这么",
    "现在",
    "时候",
    "刚才",
}

# 上下文线索扩展：从最近几轮原文补充检索线索（同义词常出现在前后文）
_CONTEXT_CUE_TURNS = 3
_MAX_QUERY_TERMS = 20


def extract_terms(text: str, max_terms: int = 12) -> list[str]:
    """提取检索线索：中文 bigram + 英文/数字词，去重、过滤停用词、限数。

    用 bigram 而非整段中文，是因为记忆 content 是另一句不同的话，
    整段子串几乎不会重合，但「北京」「出差」这类二字线索会重合——
    这正是人脑「编码特异性」式的线索触发。
    """
    if not text:
        return []
    text = _URL_RE.sub(" ", text)
    text = _PLACEHOLDER_RE.sub(" ", text)
    terms: list[str] = []
    seen: set[str] = set()

    for cjk in _CJK_RE.findall(text):
        # 对每段中文做二字滑窗
        for i in range(len(cjk) - 1):
            gram = cjk[i : i + 2]
            # 两字均为停用字（"我的"），或命中功能词表（"可以"）的组合不是有效线索
            if gram in _CJK_BIGRAM_STOPWORDS or (
                gram[0] in _STOPWORDS and gram[1] in _STOPWORDS
            ):
                continue
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


def _format_raw_time(ts: int) -> str:
    return time.strftime("%m-%d", time.localtime(ts))


def _format_memory_block(memories: list[dict]) -> str:
    lines = [
        "[长期记忆]（来自过往对话，可能已过时；仅在与当前话题相关时自然使用，不要主动罗列）"
    ]
    for mem in memories:
        if mem.get("memory_type") == "insight":
            prefix = "[洞察] "
        elif mem.get("memory_type") == "semantic":
            prefix = "[认知] "
        elif mem.get("memory_type") == "raw" or "memory_type" not in mem:
            # raw_turns 表的行没有 memory_type 字段；带时间戳便于模型判断新鲜度
            prefix = "[对话] "
            speaker = mem.get("speaker_name")
            content = mem["content"].replace("\n", " / ")
            time_hint = (
                f"[{_format_raw_time(mem['created_at'])}] "
                if mem.get("created_at")
                else ""
            )
            lines.append(
                f"- {prefix}{time_hint}{speaker + ': ' if speaker else ''}{content}"
            )
            continue
        else:
            prefix = "[往事] "
        subject = mem.get("subject")
        subject_hint = f"（{subject}）" if subject else ""
        lines.append(f"- {prefix}{mem['content']}{subject_hint}")
    return "\n".join(lines)


async def handle_recall(
    context,
    config: dict,
    store: MemoryStore,
    event: AstrMessageEvent,
    req: ProviderRequest,
) -> None:
    """on_llm_request 钩子：三层召回并注入。

    - 常驻层：importance×strength 最高的语义记忆/洞察，模拟「稳定认知」；
    - 线索层：当前发言（+ 最近上下文补充）bigram 命中的结构化记忆 + 原始对话轮次
      （原文引用更贴近真人回忆），结构化记忆命中后强化（模拟「越常被用到的记忆越难忘」）；
    - 近因层：最近几条原始轮次，仅群聊注入（私聊已有会话历史，注入重复内容）。
    """
    scope_type = "private" if event.is_private_chat() else "group"
    if scope_type == "private" and not config.get("enable_private_memory", True):
        return
    if scope_type == "group" and not config.get("enable_group_memory", True):
        return

    scope = resolve_scope(event)
    config = merge_scope_config(
        config,
        await store.get_scope_config(scope.scope_type, scope.scope_key),
        scope_type,
    )
    if not config.get("scope_enabled", True):
        return

    # 线索提取：当前句优先，最近几轮原文补充——同义词/指代常出现在前后文，
    # 线索来自整个情境而非单句才是「编码特异性」的完整实现
    query_text = req.prompt or event.message_str or ""
    terms = extract_terms(query_text)
    if len(terms) < _MAX_QUERY_TERMS:
        context_turns = await store.get_recent_raw(
            scope.scope_type, scope.scope_key, _CONTEXT_CUE_TURNS
        )
        if context_turns:
            context_text = " ".join(m["content"] for m in context_turns)
            for t in extract_terms(context_text, max_terms=_MAX_QUERY_TERMS):
                if t not in terms:
                    terms.append(t)
                if len(terms) >= _MAX_QUERY_TERMS:
                    break

    cued: list[dict] = []
    cued_raw: list[dict] = []
    if terms:
        cued = await store.search_memories(
            scope.scope_type,
            scope.scope_key,
            terms,
            int(config.get("recall_top_k", 5)),
            # 群聊整群共享记忆池：关于当前发言人的记忆更可能被需要
            boost_subject=event.get_sender_name() if scope_type == "group" else None,
        )
        if cued:
            await store.reinforce_memories([m["id"] for m in cued])
        # 私聊：req.contexts 即模型当前可见的会话历史（每轮对话对应一条原文），
        # 其中的轮次不再重复召回，线索层只检索更早的原文——否则上下文线索
        # 扩展取自最近原文，必然命中这些原文本身，注入等于浪费 token；
        # 群聊本就没有会话历史，近因层另行为其补充。
        exclude_recent = len(req.contexts) // 2 if scope_type == "private" else 0
        cued_raw = await store.search_raw(
            scope.scope_type,
            scope.scope_key,
            terms,
            top_k=3,
            exclude_recent=exclude_recent,
        )
    cued_ids = {m["id"] for m in cued}
    raw_ids = {m["id"] for m in cued_raw}

    # 常驻层（多取一些用于去重后仍能补足配额）
    core: list[dict] = []
    core_k = int(config.get("recall_core_top_k", 3))
    if core_k > 0:
        core_raw = await store.get_core_memories(
            scope.scope_type, scope.scope_key, core_k + len(cued)
        )
        core = [m for m in core_raw if m["id"] not in cued_ids][:core_k]

    # 近因层（仅群聊）
    recent: list[dict] = []
    recent_k = int(config.get("recall_recent_turns", 5))
    if scope_type == "group" and recent_k > 0:
        recent_pool = await store.get_recent_raw(
            scope.scope_type, scope.scope_key, recent_k + len(cued_raw)
        )
        recent = [m for m in recent_pool if m["id"] not in raw_ids][:recent_k]

    memories = cued + core + cued_raw + recent
    if not memories:
        return

    memory_block = _format_memory_block(memories)
    req.extra_user_content_parts.append(TextPart(text=memory_block).mark_as_temp())
