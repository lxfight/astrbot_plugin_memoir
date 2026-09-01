"""
巩固（Consolidation）与洞察（Insight）：周期性后台批量抽取。

不挂在任何消息钩子上——群聊/私聊都可能长时间没有新对话触发钩子，
所以巩固必须是主动扫描所有 scope 的独立任务，模拟"离线巩固"。

与旧版本（逐轮 LLM 编码 + 周期升华）不同，现在每个 scope 每次触发只用
一次 LLM 调用：把未处理的原始对话轮次与已有语义记忆一起交给模型，
一次性完成 提炼（update/insert/expire，含时间归一化与过期事实清理）
+ 洞察 + 群聊自我陈述桥接；积压时最多连续消化 3 批。
原始轮次本身的遗忘由 prune_raw 按 TTL + 容量上限处理。
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from .llm_helper import call_background_llm, parse_json_object
from .scope import merge_scope_config, private_scope_key
from .storage import MemoryStore

# 单次批量抽取的原始轮次上限，防止 prompt 无限膨胀；
# 积压时一个 scope 最多连续消化 _MAX_BATCHES_PER_PASS 批
_BATCH_LIMIT = 60
_MAX_BATCHES_PER_PASS = 3
_RAW_CAP_PER_SCOPE = 500

_PROMPT_TEMPLATE = """你是记忆巩固模块。今天是 {today}。下面是一段会话最近的原始对话记录，以及当前已沉淀的稳定认知。

已有的稳定认知：
{semantic_block}

最近的对话记录（每条开头方括号内是发生时间）：
{raw_block}

请完成以下提炼，把零散对话升华为稳定的长期认知：

1. semantic_ops：找出值得长期记住的稳定事实（偏好、身份、状态变化、重要事件、承诺、关系），
   对每条判断：
   - update：某条已有认知已过时，新值直接覆盖旧值（target_id 必须来自上方 [#id]）
   - insert：新增一条认知（模式重复出现，或本身是重要的独立事实）
   - expire：某条已有认知记录的时效性事件已经过期失效（出差已结束、约定已完成、
     状态已再度变化且旧值无保留价值），删除它（target_id 必须来自上方 [#id]）
   - ignore：一次性内容，直接不出现在输出里

   时间规则：涉及相对时间的表述（明天、下周、月底等）必须结合对话时间戳改写为绝对日期，
   事件型事实以 [YYYY-MM-DD] 开头，例如：用户 [2026-09-07] 前后去北京出差。
   tags 用逗号分隔，除核心关键词外必须补充口语同义表达和上位词，
   例：内容提到柴犬，tags 应写：柴犬,小狗,狗,宠物；内容提到出差，tags 写：出差,北京,工作,旅行。
2. insight：如果能归纳出比单条事实更高层次的判断（状态变化趋势、话题走向、关系进展），
   输出一条；没有则设为 null。不能是对已有认知的简单复述。
{bridge_instruction}
按以下 JSON 格式输出，不要输出多余文字：

{{
  "semantic_ops": [
    {{"action": "update", "target_id": 12, "content": "新的认知内容", "importance": 1-5}},
    {{"action": "insert", "content": "[YYYY-MM-DD] 新的认知内容", "tags": "关键词,同义表达,上位词", "subject": "事实所属的说话人名字，群聊必填，私聊填 null", "importance": 1-5}},
    {{"action": "expire", "target_id": 12}}
  ],
  "insight": {{"content": "洞察内容", "importance": 1-5}} 或 null,
  "self_statements": []
}}

没有可提炼的内容时：semantic_ops 为 []，insight 为 null。
"""

_BRIDGE_INSTRUCTION_GROUP = """3. self_statements：如果对话中有发言人在讲述自己的事（自我陈述，而非转述他人），
   且值得让该用户在与机器人私聊时也被记得，将其列入（turn_id 填该条对话的 [#id]）：

{{
  "self_statements": [
    {{"turn_id": 34, "content": "陈述句形式的事实", "sensitivity_level": "low/medium/high"}}
  ]
}}
"""

_BRIDGE_INSTRUCTION_PRIVATE = "3. self_statements：私聊场景固定为 []。\n"


def _format_time(ts: int) -> str:
    # 带年份：跨年的原文时间不得产生歧义
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _format_semantic_block(semantic_memories: list[dict]) -> str:
    lines = [
        f"[#{m['id']}] {m['content']}"
        for m in semantic_memories
        if m["memory_type"] == "semantic"
    ]
    return "\n".join(lines) if lines else "（暂无）"


def _format_raw_block(raw_turns: list[dict]) -> str:
    lines = []
    for t in raw_turns:
        speaker = f"{t['speaker_name']}: " if t.get("speaker_name") else ""
        lines.append(
            f"[#{t['id']}] [{_format_time(t['created_at'])}] {speaker}{t['content']}"
        )
    return "\n".join(lines) if lines else "（暂无）"


async def _consolidate_scope(
    context, config: dict, store: MemoryStore, scope_type: str, scope_key: str
) -> None:
    """消化一个 scope 的待抽取轮次：每批一次 LLM 调用，最多连续 _MAX_BATCHES_PER_PASS 批。

    积压时单次扫描可消化 _BATCH_LIMIT×批数 轮；LLM 失败立即停止本 scope，
    已处理批次照常推进水位，未处理的留待下轮。会话级配置覆盖在此合并
    （bridge_max_sensitivity / scope_bridge_enabled 等按会话生效）。
    """
    override = await store.get_scope_config(scope_type, scope_key)
    config = merge_scope_config(config, override, scope_type)
    if not config.get("scope_enabled", True):
        return
    for _ in range(_MAX_BATCHES_PER_PASS):
        raw_turns = await store.get_pending_raw(scope_type, scope_key, _BATCH_LIMIT)
        if not raw_turns:
            break
        semantic = await store.get_semantic_memories(scope_type, scope_key)

        bridge_instruction = (
            _BRIDGE_INSTRUCTION_GROUP
            if scope_type == "group"
            else _BRIDGE_INSTRUCTION_PRIVATE
        )
        prompt = _PROMPT_TEMPLATE.format(
            today=time.strftime("%Y-%m-%d"),
            semantic_block=_format_semantic_block(semantic),
            raw_block=_format_raw_block(raw_turns),
            bridge_instruction=bridge_instruction,
        )
        raw = await call_background_llm(context, config, prompt=prompt)
        if raw is None:
            # LLM 调用失败：不标记已抽取，留待下轮重试；停止继续消化
            return

        turn_map = {t["id"]: t for t in raw_turns}
        parsed = parse_json_object(raw)
        if isinstance(parsed, dict):
            await _apply_semantic_ops(store, scope_type, scope_key, parsed, semantic)
            await _apply_insight(store, scope_type, scope_key, parsed)
            if scope_type == "group":
                await _bridge_self_statements(
                    config, store, scope_key, parsed, turn_map
                )
        else:
            logger.warning("[Memoir] 巩固输出无法解析为 JSON，本批轮次仅标记已处理")

        # 无论抽取结果如何，只要 LLM 成功响应就推进水位，避免同一批坏输出无限重试
        await store.mark_raw_extracted([t["id"] for t in raw_turns])
        await store.cap_raw(scope_type, scope_key, _RAW_CAP_PER_SCOPE)
        await store.mark_scope_consolidated(scope_type, scope_key)


async def _apply_semantic_ops(
    store: MemoryStore,
    scope_type: str,
    scope_key: str,
    parsed: dict,
    semantic: list[dict],
) -> None:
    ops = parsed.get("semantic_ops")
    if not isinstance(ops, list):
        return
    # 只允许 update/expire 本 scope 的语义记忆，防止 LLM 幻觉 id 跨 scope 覆盖
    semantic_ids = {m["id"] for m in semantic if m["memory_type"] == "semantic"}
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("action")
        if kind == "update":
            content = str(op.get("content") or "").strip()
            try:
                target_id = int(op.get("target_id"))
            except (TypeError, ValueError):
                continue
            if not content or target_id not in semantic_ids:
                logger.debug(f"[Memoir] 忽略非法巩固 update: target_id={target_id}")
                continue
            importance = op.get("importance")
            try:
                importance = max(1, min(5, int(importance))) if importance else None
            except (TypeError, ValueError):
                importance = None
            await store.update_memory_content(target_id, content, importance)
        elif kind == "expire":
            try:
                target_id = int(op.get("target_id"))
            except (TypeError, ValueError):
                continue
            if target_id not in semantic_ids:
                logger.debug(f"[Memoir] 忽略非法巩固 expire: target_id={target_id}")
                continue
            await store.delete_memory_in_scope(target_id, scope_type, scope_key)
        elif kind == "insert":
            content = str(op.get("content") or "").strip()
            if not content:
                continue
            try:
                importance = max(1, min(5, int(op.get("importance") or 3)))
            except (TypeError, ValueError):
                importance = 3
            subject = str(op.get("subject") or "").strip() or None
            await store.insert_memory(
                scope_type=scope_type,
                scope_key=scope_key,
                memory_type="semantic",
                content=content,
                subject=subject,
                tags=str(op.get("tags") or ""),
                importance=importance,
            )


async def _apply_insight(
    store: MemoryStore, scope_type: str, scope_key: str, parsed: dict
) -> None:
    insight = parsed.get("insight")
    if not isinstance(insight, dict):
        return
    content = str(insight.get("content") or "").strip()
    if not content:
        return
    try:
        importance = max(1, min(5, int(insight.get("importance") or 4)))
    except (TypeError, ValueError):
        importance = 4
    await store.insert_memory(
        scope_type=scope_type,
        scope_key=scope_key,
        memory_type="insight",
        content=content,
        importance=importance,
    )


async def _bridge_self_statements(
    config: dict, store: MemoryStore, scope_key: str, parsed: dict, turn_map: dict
) -> None:
    """群聊自我陈述桥接：把已授权用户讲述自己的事实复制进其私聊原始轮次。

    以原文轮次形式进入对方私聊记忆管线，由其私聊巩固自然提炼，避免双写语义记忆。
    门控：全局桥接开关 AND 会话级桥接开关（scope_bridge_enabled）AND 用户授权 AND 敏感度。
    """
    if not config.get("enable_cross_scope_bridge", False):
        return
    if not config.get("scope_bridge_enabled", True):
        return
    bridge_max_sensitivity = config.get("bridge_max_sensitivity", "low")
    sensitivity_rank = {"low": 0, "medium": 1, "high": 2}
    platform = scope_key.split(":", 1)[0]

    statements = parsed.get("self_statements")
    if not isinstance(statements, list):
        return
    for item in statements:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        try:
            turn = turn_map.get(int(item.get("turn_id")))
        except (TypeError, ValueError):
            continue
        if turn is None or not turn.get("speaker_id"):
            continue
        sensitivity_level = str(item.get("sensitivity_level") or "low")
        consented = await store.is_bridge_enabled(platform, turn["speaker_id"])
        level_ok = sensitivity_rank.get(sensitivity_level, 2) <= sensitivity_rank.get(
            bridge_max_sensitivity, 0
        )
        if not (consented and level_ok):
            continue
        target_key = private_scope_key(platform, turn["speaker_id"])
        await store.insert_raw_turn(
            scope_type="private",
            scope_key=target_key,
            content=f"用户: {content}",
        )
        await store.touch_scope("private", target_key)


async def run_consolidation_pass(context, config: dict, store: MemoryStore) -> int:
    """执行一轮巩固扫描，返回处理的 scope 数量。

    触发阈值/静默时长支持会话级覆盖：每个 scope 用 merge 后的生效配置判断。
    """
    overrides = await store.get_all_scope_configs()
    now = int(time.time())

    due_scopes = []
    for row in await store.get_scope_activity():
        scope_type, scope_key = row["scope_type"], row["scope_key"]
        eff = merge_scope_config(
            config, overrides.get((scope_type, scope_key)), scope_type
        )
        if not eff.get("scope_enabled", True):
            continue
        threshold_key = (
            "consolidation_count_threshold_private"
            if scope_type == "private"
            else "consolidation_count_threshold_group"
        )
        threshold = int(eff.get(threshold_key, 20 if scope_type == "private" else 50))
        idle_seconds = int(eff.get("consolidation_idle_hours", 12)) * 3600
        pending = row["pending"]
        if pending <= 0:
            continue
        # 静默基线：优先上次巩固时间，从未巩固过则用会话创建时间
        # （否则新会话会因 last_consolidated_at=NULL 立即视为静默超时，阈值失效）
        last_ref = row["last_consolidated_at"] or row["created_at"] or now
        idle_expired = (now - last_ref) >= idle_seconds
        if pending >= threshold or idle_expired:
            due_scopes.append(row)

    for item in due_scopes:
        try:
            await _consolidate_scope(
                context, config, store, item["scope_type"], item["scope_key"]
            )
        except Exception as exc:
            logger.error(
                f"[Memoir] 巩固 scope {item['scope_type']}:{item['scope_key']} 失败: {exc}",
                exc_info=True,
            )
    return len(due_scopes)


async def run_forgetting_pass(
    config: dict, store: MemoryStore, interval_seconds: int
) -> int:
    """遗忘：语义记忆/洞察做强度衰减；原始轮次按 TTL 清理"""
    pruned = await store.prune_raw(int(config.get("raw_retention_days", 14)) * 86400)
    await store.decay_and_forget(
        decay_rate_semantic=float(config.get("decay_rate_semantic", 0.98)),
        decay_rate_insight=float(config.get("decay_rate_insight", 0.995)),
        interval_seconds=interval_seconds,
    )
    return pruned


class ConsolidationScheduler:
    """周期性后台任务：批量抽取 + 遗忘"""

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
        interval_minutes = int(
            self.config.get("consolidation_scan_interval_minutes", 30)
        )
        interval_seconds = max(60, interval_minutes * 60)
        while not self._stopping:
            try:
                await asyncio.sleep(interval_seconds)
                if self._stopping:
                    break
                processed = await run_consolidation_pass(
                    self.context, self.config, self.store
                )
                pruned = await run_forgetting_pass(
                    self.config, self.store, interval_seconds
                )
                if processed or pruned:
                    logger.info(
                        f"[Memoir] 巩固扫描完成: {processed} 个 scope 已处理, "
                        f"遗忘清理 {pruned} 条原始轮次"
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[Memoir] 巩固/遗忘周期任务异常: {exc}", exc_info=True)
