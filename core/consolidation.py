"""
巩固（Consolidation）与洞察（Insight）：周期性后台任务。

不挂在任何消息钩子上——群聊/私聊都可能长时间没有新对话触发钩子，
所以巩固必须是主动扫描所有 scope 的独立任务，模拟"离线巩固"。

流程：
1. 扫描所有 scope，找出满足『未巩固数量达阈值』或『静默超时』的 scope
2. 对每个 scope：把待巩固的情景记忆与已有语义记忆一起交给 LLM，
   判断 update / insert / ignore（当前版本：直接覆盖式更新）
3. 归纳洞察：把语义记忆 + 最近情景记忆放在一起，产出更高抽象层级的判断
4. 遗忘：对所有记忆做一次强度衰减，情景记忆强度过低时物理删除
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from .llm_helper import call_background_llm, parse_json_object
from .storage import MemoryStore

_CONSOLIDATE_PROMPT_TEMPLATE = """你是记忆巩固模块，负责把零散的情景记忆升华为稳定的认知（语义记忆）。

已有的稳定认知：
{semantic_block}

最近发生的新情景（待处理）：
{episodic_block}

请判断每条新情景应该：
1. update：更新某条已有认知（发生了变化，新事实应直接覆盖旧值）
2. insert：新增一条稳定认知（该模式已重复出现，或本身就是重要的独立事实）
3. ignore：只是一次性的、不构成稳定事实的内容

按以下 JSON 格式输出，不要输出多余文字：

{{
  "actions": [
    {{"action": "update", "target_id": 12, "content": "新的认知内容", "importance": 1-5}},
    {{"action": "insert", "content": "新的认知内容", "tags": "关键词1,关键词2", "importance": 1-5}},
    {{"action": "ignore", "episodic_id": 34}}
  ]
}}
"""

_INSIGHT_PROMPT_TEMPLATE = """你是记忆洞察模块，负责从多条稳定认知和最近情景中归纳出更高层次的判断
（例如：状态变化趋势、群体话题走向、关系进展），而不是简单复述已有事实。

稳定认知：
{semantic_block}

最近情景：
{episodic_block}

如果能归纳出有价值的新洞察（不能是对已有认知的简单重复），按以下 JSON 输出：

{{"has_insight": true/false, "content": "洞察内容", "importance": 1-5}}

如果没有值得输出的新洞察，has_insight 设为 false。
"""


def _format_semantic_block(semantic_memories: list[dict]) -> str:
    if not semantic_memories:
        return "（暂无）"
    lines = [f"[#{m['id']}] {m['content']}" for m in semantic_memories if m["memory_type"] == "semantic"]
    return "\n".join(lines) if lines else "（暂无）"


def _format_episodic_block(episodic_memories: list[dict]) -> str:
    lines = []
    for m in episodic_memories:
        subject_hint = f"（{m['subject']}）" if m.get("subject") else ""
        lines.append(f"[#{m['id']}] {m['content']}{subject_hint}")
    return "\n".join(lines)


async def _consolidate_scope(
    context, config: dict, store: MemoryStore, scope_type: str, scope_key: str
) -> None:
    episodic = await store.get_pending_episodic(scope_type, scope_key)
    if not episodic:
        return
    semantic = await store.get_semantic_memories(scope_type, scope_key)

    prompt = _CONSOLIDATE_PROMPT_TEMPLATE.format(
        semantic_block=_format_semantic_block(semantic),
        episodic_block=_format_episodic_block(episodic),
    )
    raw = await call_background_llm(context, config, prompt=prompt)
    parsed = parse_json_object(raw)
    actions = parsed.get("actions") if isinstance(parsed, dict) else None

    consolidated_ids = {m["id"] for m in episodic}
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = action.get("action")
            if kind == "update":
                target_id = action.get("target_id")
                content = str(action.get("content") or "").strip()
                if target_id and content:
                    importance = action.get("importance")
                    await store.update_memory_content(
                        int(target_id),
                        content,
                        int(importance) if importance else None,
                    )
            elif kind == "insert":
                content = str(action.get("content") or "").strip()
                if content:
                    importance = int(action.get("importance") or 3)
                    await store.insert_memory(
                        scope_type=scope_type,
                        scope_key=scope_key,
                        memory_type="semantic",
                        content=content,
                        tags=str(action.get("tags") or ""),
                        importance=max(1, min(5, importance)),
                    )
            # ignore 或未知动作：不做处理，情景记忆仍会被标记为已巩固，
            # 后续依靠遗忘机制自然衰减淘汰

    await store.mark_episodic_consolidated(list(consolidated_ids))

    # 洞察归纳：巩固之后，语义记忆已是最新状态，重新拉取
    semantic_after = await store.get_semantic_memories(scope_type, scope_key)
    insight_prompt = _INSIGHT_PROMPT_TEMPLATE.format(
        semantic_block=_format_semantic_block(semantic_after),
        episodic_block=_format_episodic_block(episodic),
    )
    insight_raw = await call_background_llm(context, config, prompt=insight_prompt)
    insight_parsed = parse_json_object(insight_raw)
    if isinstance(insight_parsed, dict) and insight_parsed.get("has_insight"):
        content = str(insight_parsed.get("content") or "").strip()
        if content:
            importance = int(insight_parsed.get("importance") or 4)
            await store.insert_memory(
                scope_type=scope_type,
                scope_key=scope_key,
                memory_type="insight",
                content=content,
                importance=max(1, min(5, importance)),
            )

    await store.mark_scope_consolidated(scope_type, scope_key)


async def run_consolidation_pass(context, config: dict, store: MemoryStore) -> int:
    """执行一轮巩固扫描，返回处理的 scope 数量"""
    count_threshold_private = int(config.get("consolidation_count_threshold_private", 8))
    count_threshold_group = int(config.get("consolidation_count_threshold_group", 30))
    idle_hours = int(config.get("consolidation_idle_hours", 12))

    due_scopes = await store.get_scopes_due_for_consolidation(
        count_threshold_private, count_threshold_group, idle_hours * 3600
    )
    for item in due_scopes:
        try:
            await _consolidate_scope(context, config, store, item["scope_type"], item["scope_key"])
        except Exception as exc:
            logger.error(
                f"[Memoir] 巩固 scope {item['scope_type']}:{item['scope_key']} 失败: {exc}",
                exc_info=True,
            )
    return len(due_scopes)


async def run_forgetting_pass(config: dict, store: MemoryStore, interval_seconds: int) -> int:
    """遗忘：对所有记忆做一次强度衰减，情景记忆强度过低则物理删除"""
    return await store.decay_and_forget(
        decay_rate_episodic=float(config.get("decay_rate_episodic", 0.85)),
        decay_rate_semantic=float(config.get("decay_rate_semantic", 0.98)),
        decay_rate_insight=float(config.get("decay_rate_insight", 0.995)),
        min_strength_to_keep=float(config.get("min_strength_to_keep", 0.05)),
        interval_seconds=interval_seconds,
    )


class ConsolidationScheduler:
    """周期性后台任务：巩固 + 遗忘"""

    def __init__(self, context, config: dict, store: MemoryStore):
        self.context = context
        self.config = config
        self.store = store
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        interval_minutes = int(self.config.get("consolidation_scan_interval_minutes", 30))
        interval_seconds = max(60, interval_minutes * 60)
        while not self._stopping:
            try:
                await asyncio.sleep(interval_seconds)
                if self._stopping:
                    break
                processed = await run_consolidation_pass(self.context, self.config, self.store)
                forgotten = await run_forgetting_pass(self.config, self.store, interval_seconds)
                if processed or forgotten:
                    logger.info(
                        f"[Memoir] 巩固扫描完成: {processed} 个 scope 已处理, "
                        f"遗忘清理 {forgotten} 条情景记忆"
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[Memoir] 巩固/遗忘周期任务异常: {exc}", exc_info=True)
