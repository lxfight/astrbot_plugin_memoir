"""
存储层：基于 aiosqlite 的记忆持久化。

不引入向量数据库。检索为双通道融合：精确通道（整句子串命中、tags/subject
等值命中，大幅加分置顶）+ 模糊通道（content/tags LIKE 子串命中数 +
结构化字段排序），零额外依赖。向量通道（sqlite-vec）为预留的实验扩展。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any

import aiosqlite
from astrbot.api import logger


def serialized(method):
    """Serialize database access and join the caller's transaction.

    Args:
        method: Storage operation to wrap.

    Returns:
        An operation that cannot interleave SQL with another task.
    """

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.transaction():
            return await method(self, *args, **kwargs)

    return wrapped


def _escape_like(text: str) -> str:
    """转义 LIKE 模式中的通配符，防用户输入干扰匹配"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# 事件型记忆的 [YYYY-MM-DD] 日期前缀（由巩固抽取在时间归一化时写入）
_EVENT_DATE_PREFIX_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]")

# 事件型记忆过期的宽限天数：事件日过了该天数后，衰减锚点从「最近激活
# 时间」切换为「事件日+宽限期」——已过期的时效性事实（出差已结束、约定
# 已到期）不应靠反复提及保持鲜活；重新确认的事件会由巩固流程更新日期前缀
_EVENT_STALE_GRACE_DAYS = 7


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    memory_key TEXT,
    subject TEXT,
    subject_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    tags TEXT,
    importance INTEGER DEFAULT 3,
    sensitivity_level TEXT DEFAULT 'low',
    sensitivity_category TEXT,
    source_type TEXT DEFAULT 'native',
    source_ref TEXT,
    strength REAL DEFAULT 1.0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expire_at INTEGER
);

-- 注：不使用 FTS5。SQLite 默认的 unicode61 tokenizer 把整段中文当作单个 token，
-- `MATCH "北京"` 之类子串查询无法命中，行为不可控。改为对 content/tags 做 LIKE
-- 子串匹配 + 结构化字段排序，零额外依赖，符合「不引入向量/复杂分词」的设计。

CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope_type, scope_key, memory_type);

-- 原始对话轮次：零成本落库，召回时可直接检索引用原文，
-- 周期巩固时由 LLM 批量抽取为语义记忆/洞察。
-- extracted=1 表示该轮已被巩固流程处理过；TTL 到期或超量后物理清理。
CREATE TABLE IF NOT EXISTS raw_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    speaker_id TEXT,
    speaker_name TEXT,
    content TEXT NOT NULL,
    extracted INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_pending
    ON raw_turns(scope_type, scope_key, extracted);

CREATE INDEX IF NOT EXISTS idx_raw_scope_time
    ON raw_turns(scope_type, scope_key, created_at);

CREATE TABLE IF NOT EXISTS scopes (
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    last_activity_at INTEGER,
    last_consolidated_at INTEGER,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS bridge_consent (
    platform TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    enabled INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (platform, sender_id)
);

-- 会话级配置覆盖：只存与全局不同的增量字段（sparse override），
-- 缺失字段回退全局默认。支持不同群聊/私聊使用不同的记忆策略。
CREATE TABLE IF NOT EXISTS scope_configs (
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    config_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope_type, scope_key)
);

-- Legacy failure records are retained and migrated once into work_items.
-- New failures quarantine their source turns until an explicit retry.
CREATE TABLE IF NOT EXISTS consolidation_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    turn_ids TEXT NOT NULL,
    llm_output TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scope_versions (
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_type, scope_key)
);
CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    raw_ids TEXT NOT NULL,
    raw_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_status ON work_items(kind, status, id);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    purpose TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    input_other INTEGER,
    input_cached INTEGER,
    output INTEGER,
    duration_ms INTEGER,
    error_type TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_time ON llm_usage(created_at, id);
CREATE INDEX IF NOT EXISTS idx_usage_scope_time ON llm_usage(scope_type, scope_key, created_at);
"""


class MemoryStore:
    """记忆存储管理器"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self.config_revision = 0
        self.generations: dict[tuple[str, str], int] = {}
        # scope_configs 仅在 WebUI 编辑时变更，而捕获/召回路径每条消息都会读取，
        # 用内存缓存换掉每条消息一次的 DB 往返；写路径负责同步失效
        self._scope_config_cache: dict[tuple[str, str], dict[str, Any]] = {}
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def transaction(self):
        """Commit a complete operation, rolling back on failure or cancellation.

        Yields:
            The store with exclusive, task-reentrant connection access.
        """
        if self._owner is asyncio.current_task():
            yield self
            return
        async with self._lock:
            self._owner = asyncio.current_task()
            try:
                yield self
                if self.connection:
                    await self.connection.commit()
            except BaseException:
                if self.connection:
                    await self.connection.rollback()
                self._scope_config_cache.clear()
                raise
            finally:
                self._owner = None

    async def initialize(self) -> None:
        self.connection = await aiosqlite.connect(self.db_path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA busy_timeout = 10000")
        await self.connection.executescript(SCHEMA_SQL)
        await self.connection.execute(
            "UPDATE llm_usage SET status='interrupted' WHERE status='running'"
        )
        # memory_key 迁移：旧库的 memories 表没有该列，CREATE TABLE IF NOT EXISTS
        # 不会补列，须先 ALTER 再建唯一索引（索引依赖该列，不能放进 SCHEMA_SQL）
        cursor = await self.connection.execute("PRAGMA table_info(memories)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "memory_key" not in columns:
            await self.connection.execute(
                "ALTER TABLE memories ADD COLUMN memory_key TEXT"
            )
            logger.info("[Memoir] 已为 memories 表补充 memory_key 列")
        if "subject_id" not in columns:
            await self.connection.execute(
                "ALTER TABLE memories ADD COLUMN subject_id TEXT NOT NULL DEFAULT ''"
            )
            await self.connection.execute(
                "UPDATE memories SET subject_id = 'legacy:' || subject WHERE scope_type = 'group' AND subject IS NOT NULL"
            )
        for name, definition in (
            ("edit_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("manually_edited_at", "INTEGER"),
        ):
            if name not in columns:
                await self.connection.execute(
                    f"ALTER TABLE memories ADD COLUMN {name} {definition}"
                )
        cursor = await self.connection.execute("PRAGMA table_info(raw_turns)")
        raw_columns = {row[1] for row in await cursor.fetchall()}
        for name, definition in (
            ("parent_id", "INTEGER"),
            ("source_kind", "TEXT NOT NULL DEFAULT 'native'"),
            ("source_meta", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in raw_columns:
                await self.connection.execute(
                    f"ALTER TABLE raw_turns ADD COLUMN {name} {definition}"
                )
        await self.connection.executescript("""
            CREATE INDEX IF NOT EXISTS idx_raw_parent ON raw_turns(parent_id);
            CREATE INDEX IF NOT EXISTS idx_raw_history
                ON raw_turns(scope_type, scope_key, created_at DESC, id DESC)
                WHERE parent_id IS NULL;
            CREATE INDEX IF NOT EXISTS idx_work_raw_latest ON work_items(raw_id, id DESC);
            CREATE TRIGGER IF NOT EXISTS raw_forward_cleanup AFTER DELETE ON raw_turns
            BEGIN
                DELETE FROM work_items WHERE raw_id=OLD.id OR raw_id=OLD.parent_id;
                DELETE FROM raw_turns WHERE parent_id=OLD.id;
            END;
        """)
        await self.connection.execute("DROP INDEX IF EXISTS idx_memories_scope_key")
        await self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_owner_key
                ON memories(scope_type, scope_key, memory_type, subject_id, memory_key)
                WHERE memory_key IS NOT NULL
            """
        )
        cursor = await self.connection.execute("PRAGMA table_info(scopes)")
        if "umo" not in {row[1] for row in await cursor.fetchall()}:
            await self.connection.execute("ALTER TABLE scopes ADD COLUMN umo TEXT")
        await self.connection.execute(
            "UPDATE work_items SET status = 'pending' WHERE kind IN ('media','forward') AND status = 'running'"
        )
        cursor = await self.connection.execute(
            "PRAGMA table_info(consolidation_failures)"
        )
        if "migrated" not in {row[1] for row in await cursor.fetchall()}:
            await self.connection.execute(
                "ALTER TABLE consolidation_failures ADD COLUMN migrated INTEGER NOT NULL DEFAULT 0"
            )
        cursor = await self.connection.execute(
            "SELECT * FROM consolidation_failures WHERE migrated=0"
        )
        for failure in await cursor.fetchall():
            await self.connection.execute(
                "INSERT INTO work_items(kind,scope_type,scope_key,raw_ids,payload,status,error,attempts,created_at,updated_at) VALUES('consolidation',?,?,?,?,'failed',?,2,?,?)",
                (
                    failure["scope_type"],
                    failure["scope_key"],
                    failure["turn_ids"],
                    json.dumps({"output": (failure["llm_output"] or "")[:4000]}),
                    "Legacy consolidation failed; retry while source turns remain available",
                    failure["created_at"],
                    failure["created_at"],
                ),
            )
            await self.connection.execute(
                "UPDATE consolidation_failures SET migrated=1 WHERE id=?",
                (failure["id"],),
            )
        # 旧版本（0.1.0）通过 LLM 逐轮抽取情景记忆，新架构改为原始轮次落库，
        # episodic 不再产生，历史数据一次性清理，由衰减机制交给遗忘流程的语义记忆替代
        cursor = await self.connection.execute(
            "DELETE FROM memories WHERE memory_type = 'episodic'"
        )
        if cursor.rowcount:
            logger.info(
                f"[Memoir] 已清理旧版情景记忆 {cursor.rowcount} 条（改用原始轮次存储）"
            )
        # 0.1.x 遗留字段：consolidated/last_hit_at/hit_count 在新架构中已无读取方
        cursor = await self.connection.execute("PRAGMA table_info(memories)")
        legacy = [
            row[1]
            for row in await cursor.fetchall()
            if row[1] in ("consolidated", "last_hit_at", "hit_count")
        ]
        if legacy:
            await self.connection.execute(
                "DROP INDEX IF EXISTS idx_memories_consolidated"
            )
            for column in legacy:
                try:
                    await self.connection.execute(
                        f"ALTER TABLE memories DROP COLUMN {column}"
                    )
                except aiosqlite.OperationalError:
                    # SQLite < 3.35 不支持 DROP COLUMN；旧列保留不影响运行
                    pass
            logger.info(f"[Memoir] 已清理旧版字段: {', '.join(legacy)}")
        await self.connection.commit()
        logger.info(f"[Memoir] 数据库初始化完成: {self.db_path}")

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    @serialized
    async def start_llm_usage(self, scope_type, scope_key, purpose, provider_id, model):
        """Persist a request before dispatch so interrupted calls remain visible.

        Args:
            scope_type: Conversation type.
            scope_key: Conversation identity.
            purpose: Plugin task category.
            provider_id: AstrBot provider ID, never credentials.
            model: Selected model name.

        Returns:
            Ledger record ID.
        """
        cursor = await self.connection.execute(
            "INSERT INTO llm_usage(created_at,scope_type,scope_key,purpose,provider_id,model) VALUES(?,?,?,?,?,?)",
            (int(time.time()), scope_type, scope_key, purpose, provider_id, model),
        )
        return cursor.lastrowid

    @serialized
    async def finish_llm_usage(
        self, record_id, status, counts, duration_ms, error_type
    ):
        """Finalize one dispatched call using reported counts or unknown values.

        Args:
            record_id: Ledger record to update.
            status: Call outcome.
            counts: Input, cached input and output counts, or three null values.
            duration_ms: Request duration in milliseconds.
            error_type: Exception class only; no payload or response text.
        """
        await self.connection.execute(
            "UPDATE llm_usage SET status=?,input_other=?,input_cached=?,output=?,duration_ms=?,error_type=? WHERE id=?",
            (status, *counts, duration_ms, error_type, record_id),
        )

    @serialized
    async def get_llm_usage(
        self,
        since,
        until,
        offset_minutes=0,
        scope=None,
        provider="",
        purpose="",
        before=0,
    ):
        """Aggregate reported tokens and return a cursor page of request details.

        Args:
            since: Inclusive Unix start time.
            until: Exclusive Unix end time.
            offset_minutes: Local timezone offset east of UTC.
            scope: Optional conversation type/key pair.
            provider: Optional exact provider ID.
            purpose: Optional task category.
            before: Exclusive ledger ID for detail pagination.

        Returns:
            Totals, daily bars, provider/task groups and up to 50 call records.
        """
        where, args = ["created_at>=?", "created_at<?"], [since, until]
        if scope:
            where += ["scope_type=?", "scope_key=?"]
            args.extend(scope)
        for column, value in (("provider_id", provider), ("purpose", purpose)):
            if value:
                where.append(f"{column}=?")
                args.append(value)
        condition = " AND ".join(where)
        metrics = """COUNT(*) AS calls, COALESCE(SUM(input_other IS NOT NULL),0) AS reported_calls,
            COALESCE(SUM(status IN ('error','cancelled','interrupted')),0) AS failed_calls,
            COALESCE(SUM(input_other),0) AS input_other,
            COALESCE(SUM(input_cached),0) AS input_cached, COALESCE(SUM(output),0) AS output"""
        cursor = await self.connection.execute(
            f"SELECT {metrics} FROM llm_usage WHERE {condition}", args
        )
        totals = dict(await cursor.fetchone())
        daily = await self.connection.execute(
            f"SELECT date(created_at+?, 'unixepoch') AS day,{metrics} FROM llm_usage WHERE {condition} GROUP BY day ORDER BY day",
            [offset_minutes * 60, *args],
        )
        result = {
            "totals": totals,
            "daily": [dict(row) for row in await daily.fetchall()],
        }
        for group in ("provider_id", "purpose"):
            cursor = await self.connection.execute(
                f"SELECT {group},{metrics} FROM llm_usage WHERE {condition} GROUP BY {group} ORDER BY SUM(COALESCE(input_other,0)+COALESCE(input_cached,0)+COALESCE(output,0)) DESC",
                args,
            )
            result[group] = [dict(row) for row in await cursor.fetchall()]
        detail_args = list(args)
        if before:
            condition += " AND id<?"
            detail_args.append(before)
        cursor = await self.connection.execute(
            f"SELECT * FROM llm_usage WHERE {condition} ORDER BY id DESC LIMIT 51",
            detail_args,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        result.update(
            items=rows[:50], next_cursor=rows[49]["id"] if len(rows) > 50 else None
        )
        return result

    @serialized
    async def browse_records(
        self,
        scope_type,
        scope_key,
        kind,
        filters,
        page=1,
        page_size=20,
        *,
        cursor_mode=False,
        before=None,
    ):
        """Browse scoped records with exact counts and event-level raw pagination.

        Args:
            scope_type: Conversation type.
            scope_key: Conversation identifier.
            kind: Either memories or raw.
            filters: Validated search and filter values.
            page: One-based requested page, clamped after deletion.
            page_size: Bounded page size.
            cursor_mode: Use keyset pagination without counts for raw history.
            before: Exclusive (created_at, id) boundary for older events.

        Returns:
            Items, total matches, and the effective page.
        """
        if kind not in {"memories", "raw"}:
            raise ValueError("Invalid record kind")
        if cursor_mode and kind != "raw":
            raise ValueError("Cursor pagination is only available for raw history")
        conditions = ["r.scope_type=?", "r.scope_key=?"]
        params = [scope_type, scope_key]
        query = str(filters.get("q") or "").strip()[:200]
        pattern = "%" + _escape_like(query) + "%"
        source = filters.get("source")
        if kind == "raw":
            conditions.append("r.parent_id IS NULL")
            if cursor_mode and before is not None:
                conditions.append("(r.created_at, r.id) < (?, ?)")
                params.extend(before)
            select = """r.*, (SELECT COUNT(*) FROM raw_turns c WHERE c.parent_id=r.id) AS chunk_count,
                COALESCE((SELECT CASE WHEN w.status='failed' THEN CASE WHEN json_extract(r.source_meta,'$.status') IN ('partial','unsupported') THEN json_extract(r.source_meta,'$.status') ELSE 'failed' END ELSE w.status END
                    FROM work_items w WHERE w.raw_id=r.id ORDER BY w.id DESC LIMIT 1),
                    json_extract(r.source_meta,'$.status'), CASE r.extracted WHEN -1 THEN 'failed' WHEN 0 THEN 'unextracted' ELSE 'complete' END) AS processing_status"""
            if query:
                conditions.append(
                    "(r.content LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM raw_turns c WHERE c.parent_id=r.id AND c.content LIKE ? ESCAPE '\\'))"
                )
                params.extend([pattern, pattern])
            if source in {"native", "forwarded"}:
                conditions.append(
                    "r.source_kind "
                    + ("=" if source == "native" else "!=")
                    + " 'native'"
                )
            sender = str(filters.get("sender") or "").strip()[:100]
            if sender:
                conditions.append(
                    "(r.speaker_id=? OR r.speaker_name LIKE ? ESCAPE '\\')"
                )
                params.extend([sender, "%" + _escape_like(sender) + "%"])
            for field, op in (("since", ">="), ("until", "<")):
                if filters.get(field):
                    conditions.append(f"r.created_at {op} ?")
                    params.append(filters[field])
            table = "raw_turns"
            ordering = "created_at DESC,id DESC"
        else:
            select, table = "r.*", "memories"
            ordering = "updated_at DESC,id DESC"
            if query:
                conditions.append(
                    "(r.content LIKE ? ESCAPE '\\' OR r.tags LIKE ? ESCAPE '\\')"
                )
                params.extend([pattern, pattern])
            if source in {"native", "forwarded"}:
                conditions.append(
                    "COALESCE(r.source_type,'native') "
                    + ("=" if source == "forwarded" else "!=")
                    + " 'forwarded'"
                )
            if filters.get("memory_type") in {"semantic", "insight"}:
                conditions.append("r.memory_type=?")
                params.append(filters["memory_type"])
            if filters.get("importance"):
                conditions.append("r.importance=?")
                params.append(filters["importance"])
            if filters.get("sort") == "importance":
                ordering = "importance DESC,updated_at DESC,id DESC"
        # History must seek the compound cursor rather than scan all root parents.
        index_hint = "INDEXED BY idx_raw_history" if cursor_mode else ""
        sql = f"SELECT {select} FROM {table} r {index_hint} WHERE {' AND '.join(conditions)}"
        if kind == "raw" and filters.get("status"):
            sql = f"SELECT * FROM ({sql}) WHERE processing_status=?"
            params.append(filters["status"])
        page_size = min(100, max(1, page_size))
        if cursor_mode:
            cursor = await self.connection.execute(
                f"{sql} ORDER BY {ordering} LIMIT ?", (*params, page_size + 1)
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            has_more = len(rows) > page_size
            items = rows[:page_size]
            return {
                "items": items,
                "has_more": has_more,
                "next_cursor": f"{items[-1]['created_at']}:{items[-1]['id']}"
                if has_more
                else None,
            }
        cursor = await self.connection.execute(f"SELECT COUNT(*) FROM ({sql})", params)
        total = (await cursor.fetchone())[0]
        page = max(1, min(page, (total + page_size - 1) // page_size))
        cursor = await self.connection.execute(
            f"{sql} ORDER BY {ordering} LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": [dict(row) for row in await cursor.fetchall()],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @serialized
    async def get_raw_event(self, turn_id, scope_type, scope_key):
        """Resolve a root or source chunk into its complete bounded event.

        Args:
            turn_id: Root or child identifier.
            scope_type: Authorized conversation type.
            scope_key: Authorized conversation identifier.

        Returns:
            Root, numerically ordered chunks and selected source ID, or None.
        """
        cursor = await self.connection.execute(
            "SELECT * FROM raw_turns WHERE id=? AND scope_type=? AND scope_key=?",
            (turn_id, scope_type, scope_key),
        )
        selected = await cursor.fetchone()
        if selected is None:
            return None
        root_id = selected["parent_id"] or selected["id"]
        cursor = await self.connection.execute(
            "SELECT * FROM raw_turns WHERE id=? AND scope_type=? AND scope_key=?",
            (root_id, scope_type, scope_key),
        )
        root = await cursor.fetchone()
        if root is None:
            return None
        cursor = await self.connection.execute(
            "SELECT * FROM raw_turns WHERE parent_id=? AND scope_type=? AND scope_key=? ORDER BY id",
            (root_id, scope_type, scope_key),
        )
        chunks = [dict(row) for row in await cursor.fetchall()]
        # Parsing budgets bound event size; sort numeric paths, including retry inserts.
        for row in chunks:
            meta = json.loads(row["source_meta"])
            path = meta.get("path", "")
            row["node_path"] = path
            row["_order"] = tuple(
                int(x) if x.isdecimal() else 1000000 for x in path.split(".")
            ) + (int(meta.get("chunk", "0:0").rsplit(":", 1)[-1]),)
        chunks.sort(key=lambda row: row.pop("_order"))
        return {"root": dict(root), "items": chunks, "selected_id": turn_id}

    @serialized
    async def edit_memory(
        self, memory_id, scope_type, scope_key, content, tags, importance, revision
    ):
        """Apply an optimistic manual edit without changing source or ownership.

        Args:
            memory_id: Selected memory identifier.
            scope_type: Authorized conversation type.
            scope_key: Authorized conversation identifier.
            content: Validated replacement text.
            tags: Validated comma-separated tags.
            importance: Importance from one to five.
            revision: Content revision seen by the editor.

        Returns:
            Whether the unchanged revision was edited.
        """
        cursor = await self.connection.execute(
            "UPDATE memories SET content=?,tags=?,importance=?,manually_edited_at=?,updated_at=?,edit_revision=edit_revision+1 WHERE id=? AND scope_type=? AND scope_key=? AND edit_revision=?",
            (
                content,
                tags,
                importance,
                int(time.time()),
                int(time.time()),
                memory_id,
                scope_type,
                scope_key,
                revision,
            ),
        )
        if cursor.rowcount != 1:
            return False
        await self.bump_revision(scope_type, scope_key)
        return True

    @serialized
    async def delete_records(self, kind, ids, scope_type, scope_key):
        """Delete a bounded selection atomically, rejecting any foreign ID.

        Args:
            kind: Either memories or raw.
            ids: Unique record identifiers, at most one hundred.
            scope_type: Authorized conversation type.
            scope_key: Authorized conversation identifier.

        Returns:
            Whether every selected record existed in this scope.
        """
        if kind not in {"memories", "raw"} or not ids or len(ids) > 100:
            return False
        table = "memories" if kind == "memories" else "raw_turns"
        placeholders = ",".join("?" for _ in ids)
        cursor = await self.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE scope_type=? AND scope_key=? AND id IN ({placeholders})",
            (scope_type, scope_key, *ids),
        )
        if (await cursor.fetchone())[0] != len(ids):
            return False
        await self.bump_revision(scope_type, scope_key)
        await self.connection.execute(
            f"DELETE FROM {table} WHERE scope_type=? AND scope_key=? AND id IN ({placeholders})",
            (scope_type, scope_key, *ids),
        )
        return True

    @serialized
    async def get_revision(self, scope_type: str, scope_key: str) -> int:
        """Read the generation used to invalidate in-flight work.

        Args:
            scope_type: Conversation type.
            scope_key: Conversation identifier.

        Returns:
            The current persistent generation, initially zero.
        """
        cursor = await self.connection.execute(
            "SELECT revision FROM scope_versions WHERE scope_type=? AND scope_key=?",
            (scope_type, scope_key),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    @serialized
    async def bump_revision(self, scope_type: str, scope_key: str) -> None:
        """Invalidate work that started before a destructive operation.

        Args:
            scope_type: Conversation type.
            scope_key: Conversation identifier.
        """
        key = (scope_type, scope_key)
        self.generations[key] = self.generations.get(key, 0) + 1
        await self.connection.execute(
            "INSERT INTO scope_versions(scope_type, scope_key, revision) VALUES(?,?,1) ON CONFLICT(scope_type,scope_key) DO UPDATE SET revision=revision+1",
            (scope_type, scope_key),
        )

    @serialized
    async def get_umo(self, scope_type: str, scope_key: str) -> str | None:
        """Resolve the last captured AstrBot session for provider selection.

        Args:
            scope_type: Conversation type.
            scope_key: Conversation identifier.

        Returns:
            The original session identifier, if captured by this version.
        """
        cursor = await self.connection.execute(
            "SELECT umo FROM scopes WHERE scope_type=? AND scope_key=?",
            (scope_type, scope_key),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    @serialized
    async def record_work(
        self,
        kind: str,
        scope_type: str,
        scope_key: str,
        raw_ids: list[int],
        *,
        payload: dict | None = None,
        error: str = "",
    ) -> int:
        """Persist media work or a quarantined consolidation failure.

        Args:
            kind: Media, forward expansion, or consolidation.
            scope_type: Conversation type.
            scope_key: Conversation identifier.
            raw_ids: Source turn identifiers.
            payload: Temporary attachment references or bounded diagnostics.
            error: Failure reason; empty means ready for processing.

        Returns:
            Work identifier.
        """
        now = int(time.time())
        if kind in {"media", "forward"} and not error:
            cursor = await self.connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE kind IN ('media','forward') AND status IN ('pending','running')"
            )
            if (await cursor.fetchone())[0] >= 32:
                error = "Media/forward queue is full; retry when capacity is available"
        cursor = await self.connection.execute(
            "INSERT INTO work_items(kind,scope_type,scope_key,raw_ids,raw_id,payload,status,error,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                kind,
                scope_type,
                scope_key,
                json.dumps(raw_ids),
                raw_ids[0] if kind in {"media", "forward"} else None,
                json.dumps(payload or {}, ensure_ascii=False),
                "failed" if error else "pending",
                error[:300],
                (payload or {}).get("attempts", 0),
                now,
                now,
            ),
        )
        return cursor.lastrowid

    @serialized
    async def retry_work(self, work_id: int, scope_type: str, scope_key: str) -> bool:
        """Requeue failed work only while its original source still exists.

        Args:
            work_id: Failed work identifier.
            scope_type: Conversation type required to authorize this operation.
            scope_key: Conversation identifier required to authorize this operation.

        Returns:
            Whether work was requeued.
        """
        cursor = await self.connection.execute(
            "SELECT * FROM work_items WHERE id=? AND scope_type=? AND scope_key=? AND status='failed'",
            (work_id, scope_type, scope_key),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        if json.loads(row["payload"]).get("retryable") is False:
            return False
        ids = json.loads(row["raw_ids"])
        if not ids:
            return False
        cursor = await self.connection.execute(
            f"SELECT COUNT(*) FROM raw_turns WHERE scope_type=? AND scope_key=? AND id IN ({','.join('?' for _ in ids)})",
            (scope_type, scope_key, *ids),
        )
        if (await cursor.fetchone())[0] != len(ids):
            return False
        if row["kind"] in {"media", "forward"}:
            cursor = await self.connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE kind IN ('media','forward') AND status IN ('pending','running')"
            )
            if (await cursor.fetchone())[0] >= 32:
                return False
            payload = json.loads(row["payload"])
            payload["revision"] = await self.get_revision(scope_type, scope_key)
            await self.connection.execute(
                "UPDATE work_items SET status='pending',error='',payload=?,updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), int(time.time()), work_id),
            )
        else:
            await self.connection.execute(
                f"UPDATE raw_turns SET extracted=0 WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
            await self.connection.execute(
                "DELETE FROM work_items WHERE id=?", (work_id,)
            )
            await self.connection.execute(
                "UPDATE scopes SET last_consolidated_at=1 WHERE scope_type=? AND scope_key=?",
                (scope_type, scope_key),
            )
        return True

    @serialized
    async def get_processing_status(self, scope_type: str, scope_key: str) -> dict:
        """Read bounded operational diagnostics without exposing media references.

        Args:
            scope_type: Conversation type.
            scope_key: Conversation identifier.

        Returns:
            Counts, oldest pending age, and recent failures for this scope.
        """
        cursor = await self.connection.execute(
            "SELECT kind,status,COUNT(*) AS count FROM work_items WHERE scope_type=? AND scope_key=? GROUP BY kind,status",
            (scope_type, scope_key),
        )
        counts = [dict(row) for row in await cursor.fetchall()]
        cursor = await self.connection.execute(
            "SELECT MIN(created_at) FROM raw_turns WHERE scope_type=? AND scope_key=? AND extracted=0",
            (scope_type, scope_key),
        )
        oldest = (await cursor.fetchone())[0]
        cursor = await self.connection.execute(
            "SELECT id,kind,status,error,attempts,created_at,updated_at,raw_ids,payload FROM work_items WHERE scope_type=? AND scope_key=? AND status='failed' ORDER BY id DESC LIMIT 100",
            (scope_type, scope_key),
        )
        failures = [dict(row) for row in await cursor.fetchall()]
        for failure in failures:
            ids = json.loads(failure.pop("raw_ids"))
            cursor = await self.connection.execute(
                f"SELECT COUNT(*) FROM raw_turns WHERE id IN ({','.join('?' for _ in ids)}) AND scope_type=? AND scope_key=?",
                (*ids, scope_type, scope_key),
            )
            failure["retryable"] = (
                json.loads(failure.pop("payload")).get("retryable", True)
                and bool(ids)
                and (await cursor.fetchone())[0] == len(ids)
            )
        cursor = await self.connection.execute(
            "SELECT id,kind,status,error,attempts,created_at,updated_at,raw_id FROM work_items WHERE scope_type=? AND scope_key=? ORDER BY id DESC LIMIT 100",
            (scope_type, scope_key),
        )
        jobs = [dict(row) for row in await cursor.fetchall()]
        retryable = {item["id"]: item["retryable"] for item in failures}
        for job in jobs:
            job["retryable"] = retryable.get(job["id"], False)
        return {
            "items": jobs,
            "counts": counts,
            "oldest_pending_seconds": max(0, int(time.time()) - oldest)
            if oldest
            else 0,
            "failures": failures,
        }

    @serialized
    async def get_memory_sources(
        self, memory_id: int, scope_type: str, scope_key: str
    ) -> dict | None:
        """Read available source turns, scoped to the selected memory.

        Args:
            memory_id: Memory identifier.
            scope_type: Conversation type.
            scope_key: Conversation identifier.

        Returns:
            Available source text and an expired count, or None if not found.
        """
        cursor = await self.connection.execute(
            "SELECT source_ref FROM memories WHERE id=? AND scope_type=? AND scope_key=?",
            (memory_id, scope_type, scope_key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            ids = json.loads(row[0] or "[]")
            ids = (
                [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []
            )
        except (ValueError, TypeError):
            ids = []
        cursor = await self.connection.execute(
            f"SELECT id,content,speaker_name,created_at,parent_id,source_kind,source_meta FROM raw_turns WHERE id IN ({','.join('?' for _ in ids)}) AND scope_type=? AND scope_key=? ORDER BY id",
            (*ids, scope_type, scope_key),
        )
        items = [dict(item) for item in await cursor.fetchall()]
        return {"items": items, "expired": len(ids) - len(items), "tracked": bool(ids)}

    # ==================== scopes ====================

    @serialized
    async def mark_scope_consolidated(self, scope_type: str, scope_key: str) -> None:
        if self.connection is None:
            return
        now = int(time.time())
        await self.connection.execute(
            "UPDATE scopes SET last_consolidated_at = ? WHERE scope_type = ? AND scope_key = ?",
            (now, scope_type, scope_key),
        )

    @serialized
    async def get_scope_activity(self) -> list[dict[str, Any]]:
        """返回所有 scope 及其待抽取原文数量（巩固触发的数据源）"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT s.scope_type, s.scope_key, s.last_activity_at, s.last_consolidated_at, s.created_at,
                   (SELECT COUNT(DISTINCT COALESCE(r.parent_id,r.id)) FROM raw_turns r
                     WHERE r.scope_type = s.scope_type AND r.scope_key = s.scope_key
                       AND r.extracted = 0) AS pending
            FROM scopes s
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ==================== memories ====================

    @serialized
    async def insert_memory(
        self,
        *,
        scope_type: str,
        scope_key: str,
        memory_type: str,
        content: str,
        subject: str | None = None,
        subject_id: str = "",
        memory_key: str | None = None,
        tags: str | None = None,
        importance: int = 3,
        sensitivity_level: str = "low",
        sensitivity_category: str | None = None,
        source_type: str = "native",
        source_ref: str | None = None,
        expire_at: int | None = None,
    ) -> int:
        """写入一条结构化记忆（semantic/insight）。情景记忆已由 raw_turns 取代。

        memory_key 是记忆条目的规范唯一索引（如 "user:job"），由巩固阶段的
        抽取模型生成。撞已有 key 时自动转为对既有条目的更新（内容覆盖 +
        重新激活），保证「更新还是新建」即使被模型判错也不会产生重复条目。

        Returns:
            新插入条目的 id；撞 key 转更新时返回既有条目的 id。
        """
        if self.connection is None:
            raise RuntimeError("数据库连接未初始化")
        if source_ref:
            try:
                source_ids = json.loads(source_ref)
                source_ids = (
                    [i for i in source_ids if type(i) is int]
                    if isinstance(source_ids, list)
                    else []
                )
            except (TypeError, ValueError):
                source_ids = []
            if source_ids:
                cursor = await self.connection.execute(
                    f"SELECT id FROM raw_turns WHERE scope_type=? AND scope_key=? AND source_kind!='native' AND id IN ({','.join('?' for _ in source_ids)}) LIMIT 1",
                    (scope_type, scope_key, *source_ids),
                )
                if await cursor.fetchone():
                    source_type = "forwarded"
        if source_type == "forwarded":
            subject, subject_id = None, ""
            memory_key = f"forward:{memory_key}" if memory_key else None
            content = "[转发引用，署名未验证，不代表用户本人] " + content
        now = int(time.time())
        cursor = await self.connection.execute(
            """
            INSERT INTO memories (
                scope_type, scope_key, memory_type, subject, subject_id, memory_key, content, tags,
                importance, sensitivity_level, sensitivity_category,
                source_type, source_ref,
                created_at, updated_at, expire_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_key, memory_type, subject_id, memory_key)
                WHERE memory_key IS NOT NULL
            DO UPDATE SET content = excluded.content, subject = excluded.subject,
                tags = excluded.tags, importance = excluded.importance,
                source_ref = excluded.source_ref, source_type = excluded.source_type,
                strength = 1.0, updated_at = excluded.updated_at, edit_revision = memories.edit_revision + 1
            """,
            (
                scope_type,
                scope_key,
                memory_type,
                subject,
                subject_id
                or (f"legacy:{subject}" if scope_type == "group" and subject else ""),
                memory_key,
                content,
                tags,
                importance,
                sensitivity_level,
                sensitivity_category,
                source_type,
                source_ref,
                now,
                now,
                expire_at,
            ),
        )
        if memory_key:
            cursor = await self.connection.execute(
                "SELECT id FROM memories WHERE scope_type=? AND scope_key=? AND memory_type=? AND subject_id=? AND memory_key=?",
                (
                    scope_type,
                    scope_key,
                    memory_type,
                    subject_id
                    or (
                        f"legacy:{subject}" if scope_type == "group" and subject else ""
                    ),
                    memory_key,
                ),
            )
            return (await cursor.fetchone())["id"]
        return cursor.lastrowid or 0

    @serialized
    async def update_memory_content(
        self,
        memory_id: int,
        content: str,
        importance: int | None = None,
        tags: str | None = None,
        memory_key: str | None = None,
        source_ref: str | None = None,
    ) -> None:
        """直接覆盖更新（当前版本的语义记忆更新策略）。

        更新即重新激活：strength 重置为 1.0 并刷新 updated_at，
        衰减从更新时刻重新起算。tags 传 None 表示保持不变；
        内容主题变化时由巩固流程传入新 tags，否则旧 tags 会让
        更新后的记忆在 tags 加权检索中失配。memory_key 传 None
        表示保持不变；非 None 时用于给无 key 的旧条目回填，
        但同 scope 下已有其他条目占用该 key 时不回填（避免唯一索引冲突）。
        """
        if self.connection is None:
            return
        now = int(time.time())
        sets = [
            "content = ?",
            "strength = 1.0",
            "updated_at = ?",
            "edit_revision = edit_revision + 1",
        ]
        params: list[Any] = [content, now]
        if source_ref is not None:
            sets.append("source_ref = ?")
            params.append(source_ref)
        if importance is not None:
            sets.append("importance = ?")
            params.append(importance)
        if tags is not None:
            sets.append("tags = ?")
            params.append(tags)
        if memory_key is not None:
            cursor = await self.connection.execute(
                """
                SELECT id FROM memories
                WHERE scope_type = (SELECT scope_type FROM memories WHERE id = ?)
                  AND scope_key = (SELECT scope_key FROM memories WHERE id = ?)
                  AND subject_id = (SELECT subject_id FROM memories WHERE id = ?)
                  AND memory_type = (SELECT memory_type FROM memories WHERE id = ?)
                  AND memory_key = ? AND id != ?
                """,
                (memory_id, memory_id, memory_id, memory_id, memory_key, memory_id),
            )
            if await cursor.fetchone() is None:
                sets.append("memory_key = ?")
                params.append(memory_key)
            else:
                logger.debug(
                    f"[Memoir] memory_key 已被其他条目占用，跳过回填: {memory_key}"
                )
        params.append(memory_id)
        await self.connection.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params
        )

    @serialized
    async def insert_raw_turn(
        self,
        *,
        scope_type: str,
        scope_key: str,
        content: str,
        speaker_id: str | None = None,
        speaker_name: str | None = None,
        umo: str | None = None,
    ) -> int:
        """原始对话轮次落库（零 LLM 成本），并在同一事务内更新会话活跃时间。

        供检索引用与周期批量抽取。scopes 记录随插入一并 upsert，
        捕获路径从「insert + touch」两次提交降为一次。
        多行内容折叠为单行（" / " 分隔）：原文会逐行拼进巩固 prompt，
        单行不变量可防止消息内容伪造 prompt 的逐行结构（[#id]/时间戳行）。

        Args:
            scope_type: 会话类型（private/group）。
            scope_key: 会话标识。
            content: 消息原文，超长截断到 2000 字符。
            speaker_id: 发言人 id（群聊），私聊为 None。
            speaker_name: 发言人昵称（群聊），私聊为 None。

        Returns:
            新插入轮次的 id。
        """
        if self.connection is None:
            raise RuntimeError("数据库连接未初始化")
        now = int(time.time())
        content = " / ".join(
            line.strip()
            for line in content.replace("\r", "").split("\n")
            if line.strip()
        )
        cursor = await self.connection.execute(
            """
            INSERT INTO raw_turns (scope_type, scope_key, speaker_id, speaker_name, content, extracted, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (scope_type, scope_key, speaker_id, speaker_name, content[:2000], now),
        )
        await self.connection.execute(
            """
            INSERT INTO scopes (scope_type, scope_key, last_activity_at, last_consolidated_at, created_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(scope_type, scope_key) DO UPDATE SET last_activity_at = excluded.last_activity_at
            """,
            (scope_type, scope_key, now, now),
        )
        if umo:
            await self.connection.execute(
                "UPDATE scopes SET umo=? WHERE scope_type=? AND scope_key=?",
                (umo, scope_type, scope_key),
            )
        return cursor.lastrowid or 0

    @serialized
    async def get_pending_raw(
        self, scope_type: str, scope_key: str, limit: int = 60
    ) -> list[dict[str, Any]]:
        """按时间正序取一批未抽取的原始轮次"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT * FROM raw_turns
            WHERE scope_type = ? AND scope_key = ? AND extracted = 0
              AND NOT EXISTS (SELECT 1 FROM work_items w WHERE w.raw_id=COALESCE(raw_turns.parent_id,raw_turns.id) AND w.kind IN ('media','forward') AND w.status IN ('pending','running'))
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def mark_raw_extracted(self, turn_ids: list[int]) -> None:
        if self.connection is None or not turn_ids:
            return
        placeholders = ",".join("?" * len(turn_ids))
        await self.connection.execute(
            f"UPDATE raw_turns SET extracted = 1 WHERE id IN ({placeholders})",
            turn_ids,
        )

    @serialized
    async def record_consolidation_failure(
        self,
        scope_type: str,
        scope_key: str,
        turn_ids: list[int],
        llm_output: str | None,
    ) -> None:
        """留存一次抽取失败的批信息，给"静默丢失"留恢复路径。

        调用时机：LLM 输出连续两次无法解析为 JSON，本批轮次即将被标记
        extracted=1 并最终按 TTL/容量清理。若不留痕，这批对话将永远
        不会进入语义记忆且无从发现。恢复方式：依据 turn_ids 从
        raw_turns（TTL 内）取回原文，或将 llm_output 人工解析后补录。

        Args:
            scope_type: 会话类型（private/group）。
            scope_key: 会话标识。
            turn_ids: 本批原始轮次 id 列表。
            llm_output: 最后一次无法解析的模型输出。
        """
        if self.connection is None:
            return
        await self.connection.execute(
            """
            INSERT INTO consolidation_failures (scope_type, scope_key, turn_ids, llm_output, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scope_type,
                scope_key,
                json.dumps(turn_ids),
                llm_output,
                int(time.time()),
            ),
        )

    @serialized
    async def get_semantic_memories(
        self, scope_type: str, scope_key: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT * FROM memories
            WHERE scope_type = ? AND scope_key = ? AND memory_type IN ('semantic', 'insight')
            ORDER BY memory_type = 'insight' DESC, importance DESC, updated_at DESC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def search_memories(
        self,
        scope_type: str,
        scope_key: str,
        query_terms: list[str],
        top_k: int = 5,
        boost_subject: str | None = None,
        phrase: str | None = None,
    ) -> list[dict[str, Any]]:
        """双通道检索：精确命中加权置顶 + 模糊线索评分排序。

        精确通道（高精度、低召回，命中即大幅加分）：
        - phrase 整句子串命中 content（用户原话在记忆中出现，最强的回忆线索）；
        - 线索与 tags 等值命中（tags 是 LLM 归一化的检索桥梁，等值命中比子串更可信）；
        - 线索与 subject 等值命中（群聊中提到某人的名字）。
        模糊通道（高召回）：content/tags 对每个线索做 LIKE 子串命中，
        相关性 = 1 + content 命中数 + 2×tags 命中数；boost_subject 非空时，
        subject 与当前发言人一致的记忆额外加 2 分（群聊共享记忆池）。
        两个通道分数相加融合，精确命中自然浮到结果顶部。
        排序：相关性 > 类型（洞察>语义>情景）> strength > importance。
        """
        if self.connection is None or not query_terms:
            return []
        terms = [t for t in query_terms if t and t.strip()]
        if not terms:
            return []
        likes = ["%" + _escape_like(t) + "%" for t in terms]
        content_hits = " + ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        # tags 可能为 NULL（未生成 tags 的记忆），NULL LIKE 结果为 NULL 会把整条
        # 相关性拖成 NULL，必须按 0 参与计算
        tag_hits = " + ".join(["(IFNULL(tags LIKE ? ESCAPE '\\', 0))"] * len(terms))
        where_clause = " OR ".join(
            ["(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"] * len(terms)
        )
        subject_boost = (
            "(CASE WHEN subject_id = ? AND subject_id != '' THEN 2 ELSE 0 END)"
        )

        # 精确通道加分项：等值/整句命中直接叠加进相关性
        exact_parts: list[str] = []
        exact_params: list[Any] = []
        if phrase:
            exact_parts.append(
                "(CASE WHEN content LIKE ? ESCAPE '\\' THEN 5 ELSE 0 END)"
            )
            exact_params.append("%" + _escape_like(phrase) + "%")
        exact_parts.extend(
            [
                "(CASE WHEN (',' || IFNULL(tags, '') || ',') LIKE ? ESCAPE '\\' THEN 3 ELSE 0 END)"
            ]
            * len(terms)
        )
        exact_params.extend("%," + _escape_like(t) + ",%" for t in terms)
        placeholders = ",".join("?" * len(terms))
        exact_parts.append(f"(CASE WHEN subject IN ({placeholders}) THEN 3 ELSE 0 END)")
        exact_params.extend(terms)
        exact_bonus = " + ".join(exact_parts)

        sql = f"""
            SELECT *,
                (1 + ({content_hits}) + 2 * ({tag_hits}) + {subject_boost}
                 + {exact_bonus}) AS relevance
            FROM memories
            WHERE ({where_clause}) AND scope_type = ? AND scope_key = ?
            ORDER BY
                relevance DESC,
                (memory_type = 'insight') DESC,
                (memory_type = 'semantic') DESC,
                strength DESC,
                importance DESC,
                updated_at DESC
            LIMIT ?
        """
        params: list[Any] = []
        params.extend(likes)  # content_hits
        params.extend(likes)  # tag_hits
        params.append(boost_subject or "")
        params.extend(exact_params)  # 精确通道
        for like in likes:  # where OR pairs
            params.extend([like, like])
        params.extend([scope_type, scope_key, top_k])
        async with self.connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def get_core_memories(
        self, scope_type: str, scope_key: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        """常驻记忆层：importance×strength 最高的语义记忆/洞察。

        模拟「对一个人/一个群体的稳定认知」，每次对话固定注入，不依赖线索命中。
        """
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT * FROM memories
            WHERE scope_type = ? AND scope_key = ? AND memory_type IN ('semantic', 'insight') AND source_type != 'forwarded'
            ORDER BY importance * strength DESC, updated_at DESC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def search_raw(
        self,
        scope_type: str,
        scope_key: str,
        query_terms: list[str],
        top_k: int = 3,
        exclude_recent: int = 0,
        phrase: str | None = None,
    ) -> list[dict[str, Any]]:
        """线索层检索原始对话轮次：精确整句命中加分 + 模糊线索命中数排序。

        原始轮次没有 tags，相关性 = 1 + content 命中线索数；
        phrase（用户当前整句）在 content 中子串命中时额外加 5 分——
        用户复述此前对话原文是最强、最精确的回忆线索。
        原文引用（「上次你说……」）比转述事实更接近真人回忆，与结构化记忆互补。

        Args:
            exclude_recent: 排除最近 N 条轮次。私聊召回传入当前会话历史
                可见的轮次数，避免把模型上下文里已有的内容重复注入。
        """
        if self.connection is None or not query_terms:
            return []
        terms = [t for t in query_terms if t and t.strip()]
        if not terms:
            return []
        likes = ["%" + _escape_like(t) + "%" for t in terms]
        content_hits = " + ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        where_clause = " OR ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        phrase_bonus = ""
        phrase_param: list[Any] = []
        if phrase:
            phrase_bonus = "+ (CASE WHEN content LIKE ? ESCAPE '\\' THEN 5 ELSE 0 END)"
            phrase_param.append("%" + _escape_like(phrase) + "%")
        exclude_clause = ""
        if exclude_recent > 0:
            exclude_clause = """
              AND id NOT IN (
                  SELECT id FROM raw_turns
                  WHERE scope_type = ? AND scope_key = ?
                  ORDER BY created_at DESC, id DESC LIMIT ?
              )"""
        sql = f"""
            SELECT *,
                (1 + ({content_hits}) {phrase_bonus}) AS relevance
            FROM raw_turns
            WHERE ({where_clause}) AND scope_type = ? AND scope_key = ?{exclude_clause}
            ORDER BY relevance DESC, created_at DESC
            LIMIT ?
        """
        params: list[Any] = []
        params.extend(likes)  # content_hits
        params.extend(phrase_param)  # 精确整句命中
        params.extend(likes)  # where OR
        params.extend([scope_type, scope_key])
        if exclude_recent > 0:
            params.extend([scope_type, scope_key, exclude_recent])
        params.append(top_k)
        async with self.connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def get_recent_raw(
        self, scope_type: str, scope_key: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """近因层：最近 K 条原始轮次（群聊召回用，补上群聊无会话历史的短板）"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT * FROM raw_turns
            WHERE scope_type = ? AND scope_key = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return list(reversed([dict(row) for row in rows]))

    @serialized
    async def prune_raw(self, ttl_seconds: int, cap_per_scope: int = 500) -> int:
        """遗忘原始轮次：超过 TTL 的全局清理；对仍保留的按 scope 容量上限裁剪。

        Returns:
            本次删除的总条数。
        """
        if self.connection is None:
            return 0
        deleted = 0
        if ttl_seconds > 0:
            cutoff = int(time.time()) - ttl_seconds
            cursor = await self.connection.execute(
                "DELETE FROM raw_turns WHERE created_at < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount or 0
        async with self.connection.execute(
            "SELECT DISTINCT scope_type, scope_key FROM raw_turns"
        ) as cursor:
            scopes = await cursor.fetchall()
        for s in scopes:
            cap_cursor = await self.connection.execute(
                """
                DELETE FROM raw_turns
                WHERE scope_type = ? AND scope_key = ? AND parent_id IS NULL AND id NOT IN (
                    SELECT id FROM raw_turns
                    WHERE scope_type = ? AND scope_key = ? AND parent_id IS NULL
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                """,
                (
                    s["scope_type"],
                    s["scope_key"],
                    s["scope_type"],
                    s["scope_key"],
                    cap_per_scope,
                ),
            )
            deleted += cap_cursor.rowcount or 0
        return deleted

    @serialized
    async def cap_raw(self, scope_type: str, scope_key: str, cap: int) -> int:
        """单 scope 原始轮次容量裁剪：只保留最近 cap 条，返回删除条数"""
        if self.connection is None:
            return 0
        cursor = await self.connection.execute(
            """
            DELETE FROM raw_turns
            WHERE scope_type = ? AND scope_key = ? AND parent_id IS NULL AND id NOT IN (
                SELECT id FROM raw_turns
                WHERE scope_type = ? AND scope_key = ? AND parent_id IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            """,
            (scope_type, scope_key, scope_type, scope_key, cap),
        )
        return cursor.rowcount or 0

    @serialized
    async def prune_semantic(self, scope_type: str, scope_key: str, cap: int) -> int:
        """结构化记忆容量裁剪：超出 cap 时按召回优先级淘汰，返回删除条数。

        与 raw_turns 的容量裁剪对应；淘汰排序与召回一致（importance×strength，
        久未更新者靠后），保证被删除的是最不可能被召回的记忆。

        Args:
            scope_type: 会话类型（private/group）。
            scope_key: 会话标识。
            cap: 保留的结构化记忆条数上限。

        Returns:
            本次删除的条数。
        """
        if self.connection is None:
            return 0
        cursor = await self.connection.execute(
            """
            DELETE FROM memories
            WHERE scope_type = ? AND scope_key = ?
              AND memory_type IN ('semantic', 'insight')
              AND id NOT IN (
                  SELECT id FROM memories
                  WHERE scope_type = ? AND scope_key = ?
                    AND memory_type IN ('semantic', 'insight')
                  ORDER BY importance * strength DESC, updated_at DESC
                  LIMIT ?
              )
            """,
            (scope_type, scope_key, scope_type, scope_key, cap),
        )
        return cursor.rowcount or 0

    @serialized
    async def get_scope_memories(
        self, scope_type: str, scope_key: str, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]:
        """按更新时间倒序分页列出 scope 内记忆，供指令查看"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT id, memory_type, memory_key, subject, subject_id, source_type, source_ref, content, tags, importance, strength, updated_at
            FROM memories
            WHERE scope_type = ? AND scope_key = ?
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (scope_type, scope_key, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def get_scope_stats(self, scope_type: str, scope_key: str) -> dict[str, int]:
        """按类型统计 scope 内记忆数量（含未抽取的原始轮次）"""
        if self.connection is None:
            return {}
        async with self.connection.execute(
            """
            SELECT memory_type, COUNT(*) AS n FROM memories
            WHERE scope_type = ? AND scope_key = ?
            GROUP BY memory_type
            """,
            (scope_type, scope_key),
        ) as cursor:
            rows = await cursor.fetchall()
        stats = {row["memory_type"]: row["n"] for row in rows}
        async with self.connection.execute(
            "SELECT COUNT(*) AS n FROM raw_turns WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row["n"]:
            stats["raw"] = row["n"]
        return stats

    @serialized
    async def list_scopes(self) -> list[dict[str, Any]]:
        """列出所有 scope 及其记忆/原文统计（按最近活跃倒序），供 WebUI 概览"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT s.scope_type, s.scope_key, s.last_activity_at, s.last_consolidated_at, s.created_at,
                   (SELECT COUNT(*) FROM memories m
                     WHERE m.scope_type = s.scope_type AND m.scope_key = s.scope_key) AS memory_count,
                   (SELECT COUNT(*) FROM raw_turns r
                     WHERE r.scope_type = s.scope_type AND r.scope_key = s.scope_key AND r.parent_id IS NULL) AS raw_count,
                   (SELECT COUNT(*) FROM raw_turns r WHERE r.scope_type=s.scope_type AND r.scope_key=s.scope_key AND r.parent_id IS NOT NULL) AS chunk_count,
                   (SELECT COUNT(DISTINCT COALESCE(r.parent_id,r.id)) FROM raw_turns r
                     WHERE r.scope_type = s.scope_type AND r.scope_key = s.scope_key
                       AND r.extracted = 0) AS pending_count
            FROM scopes s
            ORDER BY s.last_activity_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @serialized
    async def get_raw_turns(
        self, scope_type: str, scope_key: str, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """按时间倒序分页列出 scope 内原始轮次，返回 (rows, total)"""
        if self.connection is None:
            return [], 0
        async with self.connection.execute(
            "SELECT COUNT(*) AS n FROM raw_turns WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ) as cursor:
            row = await cursor.fetchone()
        total = row["n"] if row else 0
        async with self.connection.execute(
            """
            SELECT * FROM raw_turns
            WHERE scope_type = ? AND scope_key = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (scope_type, scope_key, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows], total

    @serialized
    async def count_scope_memories(self, scope_type: str, scope_key: str) -> int:
        """统计 scope 内结构化记忆总数，供 WebUI 分页"""
        if self.connection is None:
            return 0
        async with self.connection.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ) as cursor:
            row = await cursor.fetchone()
        return row["n"] if row else 0

    @serialized
    async def delete_raw_turn_in_scope(
        self, turn_id: int, scope_type: str, scope_key: str
    ) -> bool:
        """删除指定 id 且属于该 scope 的原始轮次，返回是否删除成功"""
        if self.connection is None:
            return False
        cursor = await self.connection.execute(
            "DELETE FROM raw_turns WHERE id = ? AND scope_type = ? AND scope_key = ?",
            (turn_id, scope_type, scope_key),
        )
        return (cursor.rowcount or 0) > 0

    @serialized
    async def list_bridge_consents(self) -> list[dict[str, Any]]:
        """列出全部桥接授权记录（按更新时间倒序），供 WebUI 管理"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            "SELECT platform, sender_id, enabled, updated_at FROM bridge_consent ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ==================== scope configs ====================

    @serialized
    async def get_scope_config(self, scope_type: str, scope_key: str) -> dict[str, Any]:
        """读取会话级配置覆盖，无覆盖时返回空 dict（结果缓存，写路径失效）"""
        cache_key = (scope_type, scope_key)
        if cache_key in self._scope_config_cache:
            return self._scope_config_cache[cache_key]
        if self.connection is None:
            return {}
        async with self.connection.execute(
            "SELECT config_json FROM scope_configs WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            data: dict[str, Any] = {}
        else:
            try:
                loaded = json.loads(row["config_json"])
                data = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
        self._scope_config_cache[cache_key] = data
        return data

    @serialized
    async def get_all_scope_configs(self) -> dict[tuple[str, str], dict[str, Any]]:
        """一次性读取全部会话配置覆盖，供巩固扫描按 scope 取阈值（同时刷新缓存）"""
        if self.connection is None:
            return {}
        result: dict[tuple[str, str], dict[str, Any]] = {}
        async with self.connection.execute(
            "SELECT scope_type, scope_key, config_json FROM scope_configs"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            try:
                data = json.loads(row["config_json"])
                if isinstance(data, dict):
                    result[(row["scope_type"], row["scope_key"])] = data
            except (json.JSONDecodeError, TypeError):
                continue
        self._scope_config_cache = result
        return result

    @serialized
    async def set_scope_config(
        self, scope_type: str, scope_key: str, config: dict[str, Any]
    ) -> None:
        """写入会话配置覆盖；config 为空时删除覆盖（完全继承全局）。同步维护缓存"""
        if self.connection is None:
            return
        await self.bump_revision(scope_type, scope_key)
        cache_key = (scope_type, scope_key)
        if config:
            await self.connection.execute(
                """
                INSERT INTO scope_configs (scope_type, scope_key, config_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_key) DO UPDATE SET config_json = excluded.config_json, updated_at = excluded.updated_at
                """,
                (
                    scope_type,
                    scope_key,
                    json.dumps(config, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            self._scope_config_cache[cache_key] = dict(config)
        else:
            await self.connection.execute(
                "DELETE FROM scope_configs WHERE scope_type = ? AND scope_key = ?",
                (scope_type, scope_key),
            )
            self._scope_config_cache.pop(cache_key, None)

    @serialized
    async def delete_memory_in_scope(
        self,
        memory_id: int,
        scope_type: str,
        scope_key: str,
        *,
        invalidate: bool = True,
    ) -> bool:
        """Delete a memory only within its owning conversation.

        Args:
            memory_id: Memory identifier.
            scope_type: Conversation type.
            scope_key: Conversation identifier.
            invalidate: Reject in-flight results after an explicit user deletion.

        Returns:
            Whether the selected memory was deleted.
        """
        if self.connection is None:
            return False
        if invalidate:
            await self.bump_revision(scope_type, scope_key)
        cursor = await self.connection.execute(
            "DELETE FROM memories WHERE id = ? AND scope_type = ? AND scope_key = ?",
            (memory_id, scope_type, scope_key),
        )
        return (cursor.rowcount or 0) > 0

    @serialized
    async def delete_scope_memories(self, scope_type: str, scope_key: str) -> int:
        """删除 scope 下全部记忆与原始轮次（forget_me），返回删除条数"""
        if self.connection is None:
            return 0
        await self.bump_revision(scope_type, scope_key)
        await self.connection.execute(
            "DELETE FROM work_items WHERE scope_type=? AND scope_key=?",
            (scope_type, scope_key),
        )
        await self.connection.execute(
            "DELETE FROM consolidation_failures WHERE scope_type=? AND scope_key=?",
            (scope_type, scope_key),
        )
        cursor = await self.connection.execute(
            "DELETE FROM memories WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        )
        deleted = cursor.rowcount or 0
        raw_cursor = await self.connection.execute(
            "DELETE FROM raw_turns WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        )
        deleted += raw_cursor.rowcount or 0
        await self.connection.execute(
            "DELETE FROM scopes WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        )
        return deleted

    @serialized
    async def reinforce_memories(self, memory_ids: list[int]) -> None:
        """召回命中后强化：重置强度为 1.0 并刷新激活时间（模拟越常被想起的记忆越难忘）。

        衰减由 updated_at 起算，刷新激活时间保证强化收益从强化时刻
        重新计衰减，不会被旧的激活时间一次性抵消。
        """
        if self.connection is None or not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self.connection.execute(
            f"UPDATE memories SET strength = 1.0, updated_at = ? WHERE id IN ({placeholders})",
            (int(time.time()), *memory_ids),
        )

    @serialized
    async def decay_and_forget(
        self, decay_rate_semantic: float, decay_rate_insight: float
    ) -> None:
        """按距上次激活的自然日数重算语义记忆/洞察的强度（影响排序优先级），不做物理删除。

        strength 是墙钟时间的纯函数：strength = 保留比例 ** 流逝天数，
        激活指写入、更新或召回强化（三者都会把 updated_at 刷新为当前时刻）。
        事件型记忆（[YYYY-MM-DD] 前缀）的事件日已过宽限期后，衰减天数改按
        「事件日 + 宽限期」起算并取与激活衰减的较弱者：无论是否被召回强化，
        过期事件都会持续衰减，直到被巩固流程更新（新事件日期会重置锚点）。
        与巩固扫描周期无关——调整扫描频率不会改变遗忘速度，长时间停机后的
        首次扫描也会一次性补齐期间累积的衰减；重复执行幂等，不会像逐次
        相乘那样在陈旧强度上叠加。重算结果与现值差异在容差内的行跳过写入，
        避免每个扫描周期对全部记忆做无效 UPDATE；被跳过的行会在差值累积
        超过容差后的某轮扫描中补写，排序精度不受影响。容量淘汰由
        prune_semantic 按同一优先级处理；原始轮次的遗忘由 prune_raw 按
        TTL + 容量上限处理。

        Args:
            decay_rate_semantic: 语义记忆每自然日的强度保留比例（0-1）。
            decay_rate_insight: 洞察每自然日的强度保留比例（0-1）。
        """
        if self.connection is None:
            return
        rates = {
            "semantic": max(0.0, min(1.0, decay_rate_semantic)),
            "insight": max(0.0, min(1.0, decay_rate_insight)),
        }
        now = int(time.time())
        async with self.connection.execute(
            "SELECT id, memory_type, content, updated_at, strength FROM memories "
            "WHERE memory_type IN ('semantic', 'insight')"
        ) as cursor:
            rows = await cursor.fetchall()
        updates = []
        for row in rows:
            rate = rates.get(row["memory_type"])
            if rate is None:
                continue
            days = max(0.0, (now - row["updated_at"]) / 86400.0)
            # 过期日期规则：事件型记忆的事件日已过宽限期后，衰减从
            # 「事件日+宽限期」起算，取与激活衰减的较弱者
            match = _EVENT_DATE_PREFIX_RE.match(row["content"] or "")
            if match:
                try:
                    event_ts = time.mktime(time.strptime(match.group(1), "%Y-%m-%d"))
                except ValueError:
                    event_ts = None
                if event_ts is not None:
                    event_days = (
                        now - event_ts - _EVENT_STALE_GRACE_DAYS * 86400
                    ) / 86400.0
                    if event_days > days:
                        days = event_days
            strength = rate**days
            # 差值在容差内不写：相邻扫描周期 seconds 级时间差引起的强度变化
            # 远小于排序所需的精度，全部重写只会放大 WAL 写入量
            if abs(strength - row["strength"]) < 0.001:
                continue
            updates.append((strength, row["id"]))
        if updates:
            await self.connection.executemany(
                "UPDATE memories SET strength = ? WHERE id = ?", updates
            )

    # ==================== bridge consent ====================

    @serialized
    async def is_bridge_enabled(self, platform: str, sender_id: str) -> bool:
        if self.connection is None:
            return False
        async with self.connection.execute(
            "SELECT enabled FROM bridge_consent WHERE platform = ? AND sender_id = ?",
            (platform, sender_id),
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row and row["enabled"])

    @serialized
    async def set_bridge_enabled(
        self, platform: str, sender_id: str, enabled: bool
    ) -> None:
        if self.connection is None:
            return
        now = int(time.time())
        await self.connection.execute(
            """
            INSERT INTO bridge_consent (platform, sender_id, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, sender_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (platform, sender_id, int(enabled), now),
        )
