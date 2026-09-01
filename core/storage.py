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

-- 巩固抽取失败的备份：LLM 输出连续无法解析时，本批轮次仍会标记已处理，
-- 抽取内容随之静默丢失；此处留存轮次 id 与原始输出供事后排查/人工恢复。
-- 原始轮次在 raw_turns 中保留至 TTL 清理，turn_ids 可用于定位或重放。
CREATE TABLE IF NOT EXISTS consolidation_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    turn_ids TEXT NOT NULL,
    llm_output TEXT,
    created_at INTEGER NOT NULL
);
"""


class MemoryStore:
    """记忆存储管理器"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.connection: aiosqlite.Connection | None = None
        # scope_configs 仅在 WebUI 编辑时变更，而捕获/召回路径每条消息都会读取，
        # 用内存缓存换掉每条消息一次的 DB 往返；写路径负责同步失效
        self._scope_config_cache: dict[tuple[str, str], dict[str, Any]] = {}
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
        """直接覆盖更新（当前版本的语义记忆更新策略）。

        更新即重新激活：strength 重置为 1.0 并刷新 updated_at，
        衰减从更新时刻重新起算。
        """
        if self.connection is None:
            return
        now = int(time.time())
        if importance is not None:
            await self.connection.execute(
                "UPDATE memories SET content = ?, importance = ?, strength = 1.0, updated_at = ? WHERE id = ?",
                (content, importance, now, memory_id),
            )
        else:
            await self.connection.execute(
                "UPDATE memories SET content = ?, strength = 1.0, updated_at = ? WHERE id = ?",
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
        boost_subject: str | None = None,
    ) -> list[dict[str, Any]]:
        """子串匹配检索 + 相关性评分排序（不使用向量相似度，也不依赖 FTS5 分词）。

        用当前用户发言的关键线索对记忆的 content/tags 做子串命中。
        相关性 = 1 + content 命中线索数 + 2×tags 命中线索数，tags 加权是因为
        tags 由 LLM 归一化生成，是跨措辞同义匹配（"出差"vs"去北京"）的桥梁。
        boost_subject 非空时，subject 与其一致（群聊中关于当前发言人）的记忆
        额外加 2 分：群聊是整群共享记忆池，提问者相关的事实更可能被需要。
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
        # tags 可能为 NULL（未生成 tags 的记忆），NULL LIKE 结果为 NULL 会把整条
        # 相关性拖成 NULL，必须按 0 参与计算
        tag_hits = " + ".join(["(IFNULL(tags LIKE ? ESCAPE '\\', 0))"] * len(terms))
        where_clause = " OR ".join(
            ["(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"] * len(terms)
        )
        subject_boost = "(CASE WHEN subject = ? THEN 2 ELSE 0 END)"
        sql = f"""
            SELECT *,
                (1 + ({content_hits}) + 2 * ({tag_hits}) + {subject_boost}) AS relevance
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
        params.append(boost_subject or "")  # subject_boost（空串不匹配任何 subject）
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
        exclude_recent: int = 0,
    ) -> list[dict[str, Any]]:
        """线索层检索原始对话轮次：LIKE 子串命中 + 相关性（命中线索数）> 时间新近排序。

        原始轮次没有 tags，相关性 = 1 + content 命中线索数；
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
        likes = [
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for t in terms
        ]
        content_hits = " + ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
        where_clause = " OR ".join(["(content LIKE ? ESCAPE '\\')"] * len(terms))
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
                (1 + ({content_hits})) AS relevance
            FROM raw_turns
            WHERE ({where_clause}) AND scope_type = ? AND scope_key = ?{exclude_clause}
            ORDER BY relevance DESC, created_at DESC
            LIMIT ?
        """
        params: list[Any] = []
        params.extend(likes)  # content_hits
        params.extend(likes)  # where OR
        params.extend([scope_type, scope_key])
        if exclude_recent > 0:
            params.extend([scope_type, scope_key, exclude_recent])
        params.append(top_k)
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

    async def set_scope_config(
        self, scope_type: str, scope_key: str, config: dict[str, Any]
    ) -> None:
        """写入会话配置覆盖；config 为空时删除覆盖（完全继承全局）。同步维护缓存"""
        if self.connection is None:
            return
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
        await self.connection.commit()

    async def decay_and_forget(
        self, decay_rate_semantic: float, decay_rate_insight: float
    ) -> None:
        """按距上次激活的自然日数重算语义记忆/洞察的强度（影响排序优先级），不做物理删除。

        strength 是墙钟时间的纯函数：strength = 保留比例 ** 流逝天数，
        激活指写入、更新或召回强化（三者都会把 updated_at 刷新为当前时刻）。
        与巩固扫描周期无关——调整扫描频率不会改变遗忘速度，长时间停机后的
        首次扫描也会一次性补齐期间累积的衰减；重复执行幂等，不会像逐次
        相乘那样在陈旧强度上叠加。容量淘汰由 prune_semantic 按同一优先级
        处理；原始轮次的遗忘由 prune_raw 按 TTL + 容量上限处理。

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
            "SELECT id, memory_type, updated_at FROM memories "
            "WHERE memory_type IN ('semantic', 'insight')"
        ) as cursor:
            rows = await cursor.fetchall()
        updates = []
        for row in rows:
            rate = rates.get(row["memory_type"])
            if rate is None:
                continue
            days = max(0.0, (now - row["updated_at"]) / 86400.0)
            updates.append((rate**days, row["id"]))
        if updates:
            await self.connection.executemany(
                "UPDATE memories SET strength = ? WHERE id = ?", updates
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
