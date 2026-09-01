"""
存储层：基于 aiosqlite 的记忆持久化。

不引入向量数据库。检索对 content/tags 做 LIKE 子串匹配，
按相关性（命中线索数、tags 加权）+ 结构化字段（强度 strength、重要度 importance）排序。
"""

from __future__ import annotations

import json
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

    async def get_scope_activity(self) -> list[dict[str, Any]]:
        """返回所有 scope 及其待抽取原文数量（巩固触发的数据源）"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT s.scope_type, s.scope_key, s.last_activity_at, s.last_consolidated_at, s.created_at,
                   (SELECT COUNT(*) FROM raw_turns r
                     WHERE r.scope_type = s.scope_type AND r.scope_key = s.scope_key
                       AND r.extracted = 0) AS pending
            FROM scopes s
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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
        """写入一条结构化记忆（semantic/insight）。情景记忆已由 raw_turns 取代。"""
        if self.connection is None:
            raise RuntimeError("数据库连接未初始化")
        now = int(time.time())
        cursor = await self.connection.execute(
            """
            INSERT INTO memories (
                scope_type, scope_key, memory_type, subject, content, tags,
                importance, sensitivity_level, sensitivity_category,
                source_type, source_ref,
                created_at, updated_at, expire_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_type,
                scope_key,
                memory_type,
                subject,
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

    async def insert_raw_turn(
        self,
        *,
        scope_type: str,
        scope_key: str,
        content: str,
        speaker_id: str | None = None,
        speaker_name: str | None = None,
    ) -> int:
        """原始对话轮次落库（零 LLM 成本），供检索引用与周期批量抽取"""
        if self.connection is None:
            raise RuntimeError("数据库连接未初始化")
        now = int(time.time())
        cursor = await self.connection.execute(
            """
            INSERT INTO raw_turns (scope_type, scope_key, speaker_id, speaker_name, content, extracted, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (scope_type, scope_key, speaker_id, speaker_name, content[:2000], now),
        )
        await self.connection.commit()
        return cursor.lastrowid or 0

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
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_raw_extracted(self, turn_ids: list[int]) -> None:
        if self.connection is None or not turn_ids:
            return
        placeholders = ",".join("?" * len(turn_ids))
        await self.connection.execute(
            f"UPDATE raw_turns SET extracted = 1 WHERE id IN ({placeholders})",
            turn_ids,
        )
        await self.connection.commit()

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
        """子串匹配检索 + 相关性评分排序（不使用向量相似度，也不依赖 FTS5 分词）。

        用当前用户发言的关键线索对记忆的 content/tags 做子串命中。
        相关性 = 1 + content 命中线索数 + 2×tags 命中线索数，tags 加权是因为
        tags 由 LLM 归一化生成，是跨措辞同义匹配（"出差"vs"去北京"）的桥梁。
        排序：相关性 > 类型（洞察>语义>情景）> strength > importance。
        WHERE 条件保证每条结果至少命中一个线索。
        """
        if self.connection is None or not query_terms:
            return []
        terms = [t for t in query_terms if t and t.strip()]
        if not terms:
            return []
        likes = [
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for t in terms
        ]
        content_hits = " + ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        tag_hits = " + ".join(["(tags LIKE ? ESCAPE '\\')"] * len(terms))
        where_clause = " OR ".join(
            ["(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"] * len(terms)
        )
        sql = f"""
            SELECT *,
                (1 + ({content_hits}) + 2 * ({tag_hits})) AS relevance
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
        for like in likes:  # where OR pairs
            params.extend([like, like])
        params.extend([scope_type, scope_key, top_k])
        async with self.connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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
            WHERE scope_type = ? AND scope_key = ? AND memory_type IN ('semantic', 'insight')
            ORDER BY importance * strength DESC, updated_at DESC
            LIMIT ?
            """,
            (scope_type, scope_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def search_raw(
        self,
        scope_type: str,
        scope_key: str,
        query_terms: list[str],
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """线索层检索原始对话轮次：LIKE 子串命中 + 相关性（命中线索数）> 时间新近排序。

        原始轮次没有 tags，相关性 = 1 + content 命中线索数；
        原文引用（「上次你说……」）比转述事实更接近真人回忆，与结构化记忆互补。
        """
        if self.connection is None or not query_terms:
            return []
        terms = [t for t in query_terms if t and t.strip()]
        if not terms:
            return []
        likes = [
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for t in terms
        ]
        content_hits = " + ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        where_clause = " OR ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        sql = f"""
            SELECT *,
                (1 + ({content_hits})) AS relevance
            FROM raw_turns
            WHERE ({where_clause}) AND scope_type = ? AND scope_key = ?
            ORDER BY relevance DESC, created_at DESC
            LIMIT ?
        """
        params: list[Any] = []
        params.extend(likes)  # content_hits
        params.extend(likes)  # where OR
        params.extend([scope_type, scope_key, top_k])
        async with self.connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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

    async def prune_raw(self, ttl_seconds: int, cap_per_scope: int = 500) -> int:
        """遗忘原始轮次：超过 TTL 的全局清理；对仍保留的按 scope 容量上限裁剪。

        Returns:
            本次删除的总条数。
        """
        if self.connection is None:
            return 0
        cutoff = int(time.time()) - max(ttl_seconds, 3600)
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
                WHERE scope_type = ? AND scope_key = ? AND id NOT IN (
                    SELECT id FROM raw_turns
                    WHERE scope_type = ? AND scope_key = ?
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
        await self.connection.commit()
        return deleted

    async def cap_raw(self, scope_type: str, scope_key: str, cap: int) -> int:
        """单 scope 原始轮次容量裁剪：只保留最近 cap 条，返回删除条数"""
        if self.connection is None:
            return 0
        cursor = await self.connection.execute(
            """
            DELETE FROM raw_turns
            WHERE scope_type = ? AND scope_key = ? AND id NOT IN (
                SELECT id FROM raw_turns
                WHERE scope_type = ? AND scope_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            """,
            (scope_type, scope_key, scope_type, scope_key, cap),
        )
        await self.connection.commit()
        return cursor.rowcount or 0

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
        await self.connection.commit()
        return cursor.rowcount or 0

    async def get_scope_memories(
        self, scope_type: str, scope_key: str, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]:
        """按更新时间倒序分页列出 scope 内记忆，供指令查看"""
        if self.connection is None:
            return []
        async with self.connection.execute(
            """
            SELECT id, memory_type, subject, content, tags, importance, strength, updated_at
            FROM memories
            WHERE scope_type = ? AND scope_key = ?
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (scope_type, scope_key, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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
                     WHERE r.scope_type = s.scope_type AND r.scope_key = s.scope_key) AS raw_count,
                   (SELECT COUNT(*) FROM raw_turns r
                     WHERE r.scope_type = s.scope_type AND r.scope_key = s.scope_key
                       AND r.extracted = 0) AS pending_count
            FROM scopes s
            ORDER BY s.last_activity_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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
        await self.connection.commit()
        return (cursor.rowcount or 0) > 0

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

    async def get_scope_config(self, scope_type: str, scope_key: str) -> dict[str, Any]:
        """读取会话级配置覆盖，无覆盖时返回空 dict"""
        if self.connection is None:
            return {}
        async with self.connection.execute(
            "SELECT config_json FROM scope_configs WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["config_json"])
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    async def get_all_scope_configs(self) -> dict[tuple[str, str], dict[str, Any]]:
        """一次性读取全部会话配置覆盖，供巩固扫描按 scope 取阈值"""
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
        return result

    async def set_scope_config(
        self, scope_type: str, scope_key: str, config: dict[str, Any]
    ) -> None:
        """写入会话配置覆盖；config 为空时删除覆盖（完全继承全局）"""
        if self.connection is None:
            return
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
        else:
            await self.connection.execute(
                "DELETE FROM scope_configs WHERE scope_type = ? AND scope_key = ?",
                (scope_type, scope_key),
            )
        await self.connection.commit()

    async def delete_memory_in_scope(
        self, memory_id: int, scope_type: str, scope_key: str
    ) -> bool:
        """删除指定 id 且属于该 scope 的记忆，返回是否删除成功（防止跨 scope 删除）"""
        if self.connection is None:
            return False
        cursor = await self.connection.execute(
            "DELETE FROM memories WHERE id = ? AND scope_type = ? AND scope_key = ?",
            (memory_id, scope_type, scope_key),
        )
        await self.connection.commit()
        return (cursor.rowcount or 0) > 0

    async def delete_scope_memories(self, scope_type: str, scope_key: str) -> int:
        """删除 scope 下全部记忆与原始轮次（forget_me），返回删除条数"""
        if self.connection is None:
            return 0
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
        await self.connection.commit()
        return deleted

    async def reinforce_memories(self, memory_ids: list[int]) -> None:
        """召回命中后强化：重置强度为 1.0（模拟越常被想起的记忆越难忘）"""
        if self.connection is None or not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        await self.connection.execute(
            f"UPDATE memories SET strength = 1.0 WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self.connection.commit()

    async def decay_and_forget(
        self, decay_rate_semantic: float, decay_rate_insight: float
    ) -> None:
        """对语义记忆/洞察按类型施加一次指数衰减（影响排序优先级），不做物理删除。

        每个巩固周期把 strength 乘以一次保留比例，与配置项描述一致。
        插入/更新/召回强化都会把 strength 重置为 1.0，因此最近活跃的
        记忆从下个周期起才重新衰减，强化收益不会被旧的 updated_at 抵消。

        原始轮次的遗忘由 prune_raw 按 TTL + 容量上限处理，更贴近真实遗忘行为。

        Args:
            decay_rate_semantic: 语义记忆每个周期的强度保留比例（0-1）。
            decay_rate_insight: 洞察每个周期的强度保留比例（0-1）。
        """
        if self.connection is None:
            return
        for memory_type, rate in (
            ("semantic", decay_rate_semantic),
            ("insight", decay_rate_insight),
        ):
            clamped_rate = max(0.0, min(1.0, rate))
            await self.connection.execute(
                "UPDATE memories SET strength = strength * ? WHERE memory_type = ?",
                (clamped_rate, memory_type),
            )
        await self.connection.commit()

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
