"""
存储层：基于 aiosqlite 的记忆持久化。

不引入向量数据库。检索依赖 SQLite FTS5 全文匹配 + 结构化字段排序
（重要性 importance、命中强度 strength、最近命中时间 last_hit_at）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    subject TEXT,
    content TEXT NOT NULL,
    tags TEXT,
    importance INTEGER DEFAULT 3,
    sensitivity_level TEXT DEFAULT 'low',
    sensitivity_category TEXT,
    source_type TEXT DEFAULT 'native',
    source_ref TEXT,
    strength REAL DEFAULT 1.0,
    consolidated INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_hit_at INTEGER,
    hit_count INTEGER DEFAULT 0,
    expire_at INTEGER
);

-- 注：不使用 FTS5。SQLite 默认的 unicode61 tokenizer 把整段中文当作单个 token，
-- `MATCH "北京"` 之类子串查询无法命中，行为不可控。改为对 content/tags 做 LIKE
-- 子串匹配 + 结构化字段排序，零额外依赖，符合「不引入向量/复杂分词」的设计。

CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope_type, scope_key, memory_type);

CREATE INDEX IF NOT EXISTS idx_memories_consolidated
    ON memories(scope_type, scope_key, consolidated)
    WHERE consolidated = 0;

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
"""


class MemoryStore:
    """记忆存储管理器"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.connection: aiosqlite.Connection | None = None
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        self.connection = await aiosqlite.connect(self.db_path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA busy_timeout = 10000")
        await self.connection.executescript(SCHEMA_SQL)
        await self.connection.commit()
        logger.info(f"[Memoir] 数据库初始化完成: {self.db_path}")

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    # ==================== scopes ====================

    async def touch_scope(self, scope_type: str, scope_key: str) -> None:
        """更新 scope 最近活跃时间，不存在则创建"""
        if self.connection is None:
            return
        now = int(time.time())
        await self.connection.execute(
            """
            INSERT INTO scopes (scope_type, scope_key, last_activity_at, last_consolidated_at, created_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(scope_type, scope_key) DO UPDATE SET last_activity_at = excluded.last_activity_at
            """,
            (scope_type, scope_key, now, now),
        )
        await self.connection.commit()

    async def mark_scope_consolidated(self, scope_type: str, scope_key: str) -> None:
        if self.connection is None:
            return
        now = int(time.time())
        await self.connection.execute(
            "UPDATE scopes SET last_consolidated_at = ? WHERE scope_type = ? AND scope_key = ?",
            (now, scope_type, scope_key),
        )
        await self.connection.commit()

    async def get_scopes_due_for_consolidation(
        self,
        count_threshold_private: int,
        count_threshold_group: int,
        idle_seconds: int,
    ) -> list[dict[str, Any]]:
        """扫描所有 scope，返回满足『未巩固数量达阈值』或『静默超时且有未巩固内容』的 scope 列表"""
        if self.connection is None:
            return []
        now = int(time.time())
        async with self.connection.execute(
            """
            SELECT s.scope_type, s.scope_key, s.last_consolidated_at,
                   (SELECT COUNT(*) FROM memories m
                     WHERE m.scope_type = s.scope_type AND m.scope_key = s.scope_key
                       AND m.memory_type = 'episodic' AND m.consolidated = 0) AS pending
            FROM scopes s
            """
        ) as cursor:
            rows = await cursor.fetchall()

        due = []
        for row in rows:
            pending = row["pending"]
            if pending <= 0:
                continue
            threshold = (
                count_threshold_private
                if row["scope_type"] == "private"
                else count_threshold_group
            )
            last_consolidated = row["last_consolidated_at"] or 0
            idle_expired = (now - last_consolidated) >= idle_seconds
            if pending >= threshold or idle_expired:
                due.append(
                    {
                        "scope_type": row["scope_type"],
                        "scope_key": row["scope_key"],
                        "pending": pending,
                    }
                )
        return due

    # ==================== memories ====================

    async def insert_memory(
        self,
        *,
        scope_type: str,
        scope_key: str,
        memory_type: str,
        content: str,
        subject: str | None = None,
        tags: str | None = None,
        importance: int = 3,
        sensitivity_level: str = "low",
        sensitivity_category: str | None = None,
        source_type: str = "native",
        source_ref: str | None = None,
        expire_at: int | None = None,
    ) -> int:
        if self.connection is None:
            raise RuntimeError("数据库连接未初始化")
        now = int(time.time())
        cursor = await self.connection.execute(
            """
            INSERT INTO memories (
                scope_type, scope_key, memory_type, subject, content, tags,
                importance, sensitivity_level, sensitivity_category,
                source_type, source_ref, strength, consolidated,
                created_at, updated_at, expire_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 0, ?, ?, ?)
            """,
            (
                scope_type, scope_key, memory_type, subject, content, tags,
                importance, sensitivity_level, sensitivity_category,
                source_type, source_ref, now, now, expire_at,
            ),
        )
        await self.connection.commit()
        return cursor.lastrowid or 0

    async def update_memory_content(
        self, memory_id: int, content: str, importance: int | None = None
    ) -> None:
        """直接覆盖更新（当前版本的语义记忆更新策略）"""
        if self.connection is None:
            return
        now = int(time.time())
        if importance is not None:
            await self.connection.execute(
                "UPDATE memories SET content = ?, importance = ?, updated_at = ? WHERE id = ?",
                (content, importance, now, memory_id),
            )
        else:
            await self.connection.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, memory_id),
            )
        await self.connection.commit()

    async def mark_episodic_consolidated(self, memory_ids: list[int]) -> None:
        if self.connection is None or not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self.connection.execute(
            f"UPDATE memories SET consolidated = 1 WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self.connection.commit()

    async def delete_memories(self, memory_ids: list[int]) -> None:
        if self.connection is None or not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self.connection.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self.connection.commit()

    async def get_pending_episodic(
        self, scope_type: str, scope_key: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT * FROM memories
            WHERE scope_type = ? AND scope_key = ? AND memory_type = 'episodic' AND consolidated = 0
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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

    async def search_memories(
        self,
        scope_type: str,
        scope_key: str,
        query_terms: list[str],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """子串匹配检索 + 结构化字段排序（不使用向量相似度，也不依赖 FTS5 分词）。

        用当前用户发言的关键词对 content/tags 做 LIKE 子串匹配。
        对中文友好：无需分词，整词命中即可。
        排序优先级：洞察 > 语义记忆 > 情景记忆，同类型内 importance 降序、strength 降序。
        """
        if self.connection is None or not query_terms:
            return []
        terms = [t for t in query_terms if t and t.strip()]
        if not terms:
            return []
        where_clauses = []
        params: list[Any] = []
        for term in terms:
            where_clauses.append("(content LIKE ? OR tags LIKE ?)")
            like = f"%{term}%"
            params.extend([like, like])
        where_sql = f"({' OR '.join(where_clauses)}) AND scope_type = ? AND scope_key = ?"
        params.extend([scope_type, scope_key])
        async with self.connection.execute(
            f"""
            SELECT * FROM memories
            WHERE {where_sql}
            ORDER BY
                (memory_type = 'insight') DESC,
                (memory_type = 'semantic') DESC,
                importance DESC,
                strength DESC,
                updated_at DESC
            LIMIT ?
            """,
            (*params, top_k),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def reinforce_memories(self, memory_ids: list[int]) -> None:
        """召回命中后强化：重置强度、更新命中时间与次数"""
        if self.connection is None or not memory_ids:
            return
        now = int(time.time())
        placeholders = ",".join("?" * len(memory_ids))
        await self.connection.execute(
            f"""
            UPDATE memories
            SET strength = 1.0, last_hit_at = ?, hit_count = hit_count + 1
            WHERE id IN ({placeholders})
            """,
            (now, *memory_ids),
        )
        await self.connection.commit()

    async def decay_and_forget(
        self,
        decay_rate_episodic: float,
        decay_rate_semantic: float,
        decay_rate_insight: float,
        min_strength_to_keep: float,
        interval_seconds: int,
    ) -> int:
        """对所有记忆按类型施加一次指数衰减，并删除强度过低的情景记忆（真正遗忘）。
        语义记忆/洞察不做物理删除，只衰减强度（影响排序优先级）。

        衰减强度按「距上次巩固/更新的扫描周期数」计算：
        strength *= rate^(elapsed_periods)，rate 为每个周期的保留比例。
        """
        if self.connection is None:
            return 0
        now = int(time.time())
        interval = max(interval_seconds, 1)
        for memory_type, rate in (
            ("episodic", decay_rate_episodic),
            ("semantic", decay_rate_semantic),
            ("insight", decay_rate_insight),
        ):
            clamped_rate = max(0.0, min(1.0, rate))
            await self.connection.execute(
                """
                UPDATE memories
                SET strength = strength * pow(?, (? - coalesce(updated_at, created_at)) / ?)
                WHERE memory_type = ?
                """,
                (clamped_rate, now, interval, memory_type),
            )

        async with self.connection.execute(
            "SELECT id FROM memories WHERE memory_type = 'episodic' AND strength < ?",
            (min_strength_to_keep,),
        ) as cursor:
            rows = await cursor.fetchall()
        forgotten_ids = [row["id"] for row in rows]
        if forgotten_ids:
            await self.delete_memories(forgotten_ids)
        await self.connection.commit()
        return len(forgotten_ids)

    # ==================== bridge consent ====================

    async def is_bridge_enabled(self, platform: str, sender_id: str) -> bool:
        if self.connection is None:
            return False
        async with self.connection.execute(
            "SELECT enabled FROM bridge_consent WHERE platform = ? AND sender_id = ?",
            (platform, sender_id),
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row and row["enabled"])

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
        await self.connection.commit()
