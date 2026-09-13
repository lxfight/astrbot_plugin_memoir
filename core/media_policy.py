"""Shared media controls and durable, atomic admission accounting."""

import asyncio
import time

# Values are (default, label, choices or maximum). Zero quotas mean unlimited.
MEDIA_FIELDS = {
    "image_mode": ("auto", "图片处理方式", ("auto", "manual", "off")),
    "audio_mode": ("auto", "音频处理方式", ("auto", "manual", "off")),
    "image_group_trigger": ("all", "群聊图片范围", ("all", "reply")),
    "audio_group_trigger": ("all", "群聊音频范围", ("all", "reply")),
    "image_forward_mode": ("follow", "转发图片", ("follow", "manual", "off")),
    "audio_forward_mode": ("follow", "转发音频", ("follow", "manual", "off")),
    "media_require_model": (False, "必须使用指定媒体模型", None),
    "image_max_count": (4, "每条消息图片上限", 4),
    "audio_max_count": (4, "每条消息音频上限", 4),
    "image_max_edge": (0, "图片最长边（像素，0 不缩放）", 8192),
    "image_max_mb": (10, "单张图片上限（MiB）", 10),
    "audio_max_mb": (10, "单段音频上限（MiB）", 10),
    "audio_max_seconds": (0, "音频时长上限（秒，0 不限）", 3600),
    "image_detail": ("detailed", "图片转述方式", ("brief", "detailed")),
    "audio_detail": ("detailed", "音频转述方式", ("brief", "detailed")),
    "media_text_chars": (2000, "随附文字字符上限", 2000),
    "media_output_tokens": (0, "输出长度目标（Token，仅提示词要求）", 8192),
    "media_max_requests": (4, "每条消息请求上限", 4),
    "media_timeout_seconds": (30, "单次请求超时（秒）", 90),
    "media_cache_days": (0, "成功缓存天数（0 关闭）", 365),
    "media_cache_entries": (1000, "缓存容量（全插件条数）", 10000),
    "media_daily_requests": (0, "每日多媒体请求上限", 100000),
    "image_daily_requests": (0, "每日图片请求上限", 100000),
    "audio_daily_requests": (0, "每日音频请求上限", 100000),
    "image_daily_count": (0, "每日图片数量上限", 100000),
    "audio_daily_seconds": (0, "每日音频秒数上限", 8640000),
    "media_daily_tokens": (0, "每日多媒体 Token 预算", 1000000000),
    "image_daily_tokens": (0, "每日图片 Token 预算", 1000000000),
    "audio_daily_tokens": (0, "每日音频 Token 预算", 1000000000),
    "media_token_reserve": (4096, "单次预占 Token（估算）", 1000000),
    "media_strict_budget": (False, "未知用量暂停后续调用", None),
    "media_day_offset": (480, "预算时区 UTC 偏移（分钟）", 840),
}
MEDIA_DEFAULTS = {k: v[0] for k, v in MEDIA_FIELDS.items()}
MEDIA_SCOPE_KEYS = tuple(
    k for k in MEDIA_FIELDS if k not in {"media_day_offset", "media_cache_entries"}
) + ("image_llm_provider", "audio_llm_provider")

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_budget (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
 scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, kind TEXT NOT NULL,
 images INTEGER NOT NULL, seconds INTEGER NOT NULL, tokens INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'running', acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_budget_time ON media_budget(created_at,scope_type,scope_key);
CREATE TABLE IF NOT EXISTS media_cache (
 scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, cache_key TEXT NOT NULL,
 text TEXT NOT NULL, expires INTEGER NOT NULL, used_at INTEGER NOT NULL,
 PRIMARY KEY(scope_type,scope_key,cache_key)
);
CREATE TABLE IF NOT EXISTS media_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
 scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_decisions_time ON media_decisions(created_at);
"""


class MediaGate:
    """Serialize equal media and reserve quotas before dispatching a request."""

    def __init__(self, store):
        self.store = store
        # A bounded striped lock avoids retaining a lock for every attachment.
        self.locks = [asyncio.Lock() for _ in range(32)]

    async def snapshot(self, base, scope=None, effective=None):
        """Read current-day admission totals and limits.

        Args:
            base: Global plugin configuration.
            scope: Optional conversation identity.
            effective: Effective conversation configuration.

        Returns:
            Global and optional conversation usage, with a common reset time.
        """
        cfg = {**MEDIA_DEFAULTS, **base}
        now = int(time.time())
        offset = int(cfg["media_day_offset"]) * 60
        since = (now + offset) // 86400 * 86400 - offset
        output = {"reset_at": since + 86400, "offset": offset // 60, "buckets": []}
        async with self.store.transaction():
            for identity, settings in [(None, cfg)] + (
                [(scope, {**cfg, **(effective or {})})] if scope else []
            ):
                condition, params = "created_at>=?", [since]
                if identity:
                    condition += " AND scope_type=? AND scope_key=?"
                    params.extend(identity)
                cursor = await self.store.connection.execute(
                    f"SELECT kind,COUNT(*) AS requests,SUM(images) AS images,SUM(seconds) AS seconds,SUM(tokens) AS tokens,SUM(status IN ('unknown','interrupted') AND acknowledged=0) AS unknown FROM media_budget WHERE {condition} GROUP BY kind",
                    params,
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                cursor = await self.store.connection.execute(
                    "SELECT COUNT(*) FROM media_budget WHERE status IN ('unknown','interrupted') AND acknowledged=0"
                    + (" AND scope_type=? AND scope_key=?" if identity else ""),
                    identity or (),
                )
                unresolved = (await cursor.fetchone())[0]
                limits = {k: settings[k] for k in MEDIA_FIELDS if "daily" in k}
                output["buckets"].append(
                    {
                        "scope": identity,
                        "usage": rows,
                        "limits": limits,
                        "unresolved": unresolved,
                    }
                )
            cursor = await self.store.connection.execute(
                "SELECT reason,COUNT(*) AS count FROM media_decisions WHERE created_at>=?"
                + (" AND scope_type=? AND scope_key=?" if scope else "")
                + " GROUP BY reason",
                (since, *(scope or ())),
            )
            output["decisions"] = [dict(r) for r in await cursor.fetchall()]
        return output

    async def reserve(self, config, scope, kinds, images, seconds):
        """Atomically test every applicable quota and reserve one request.

        Args:
            config: Effective settings retaining global limits separately.
            scope: Conversation identity.
            kinds: Media kinds included in this call.
            images: Actual number of submitted images.
            seconds: Rounded-up submitted audio duration.

        Returns:
            A reservation ID and empty reason, or None and a blocking reason.
        """
        cfg = {**MEDIA_DEFAULTS, **config}
        base = cfg.get("_media_global", cfg)
        reserve = max(
            1, int(cfg["media_token_reserve"]), int(cfg["media_output_tokens"])
        )
        async with self.store.transaction():
            state = await self.snapshot(base, scope, cfg)
            for bucket in state["buckets"]:
                rows = bucket["usage"]
                settings = cfg if bucket["scope"] else {**MEDIA_DEFAULTS, **base}
                if settings["media_strict_budget"] and bucket["unresolved"]:
                    return None, "budget: unknown usage requires review"
                for prefix in ("media", *sorted(kinds)):
                    relevant = (
                        rows
                        if prefix == "media"
                        else [r for r in rows if r["kind"] in (prefix, "mixed")]
                    )
                    additions = {"requests": 1, "tokens": reserve}
                    if prefix == "image":
                        additions["count"] = images
                    if prefix == "audio":
                        additions["seconds"] = seconds
                    for metric, amount in additions.items():
                        limit = int(settings.get(f"{prefix}_daily_{metric}", 0))
                        used = sum(
                            r["images" if metric == "count" else metric]
                            for r in relevant
                        )
                        if limit and used + amount > limit:
                            return None, f"budget: {prefix} daily {metric} limit"
            cursor = await self.store.connection.execute(
                "INSERT INTO media_budget(created_at,scope_type,scope_key,kind,images,seconds,tokens) VALUES(?,?,?,?,?,?,?)",
                (
                    int(time.time()),
                    *scope,
                    next(iter(kinds)) if len(kinds) == 1 else "mixed",
                    images,
                    seconds,
                    reserve,
                ),
            )
            return cursor.lastrowid, ""

    async def settle(self, reservation, response):
        """Settle reported usage, preserving reservations for unknown calls.

        Args:
            reservation: Admission record identifier.
            response: Provider response or None on cancellation or failure.
        """
        usage = getattr(response, "usage", None)
        values = [
            getattr(usage, k, None) for k in ("input_other", "input_cached", "output")
        ]
        known = all(type(v) is int and 0 <= v < 2**53 for v in values)
        async with self.store.transaction():
            await self.store.connection.execute(
                "UPDATE media_budget SET status=?,tokens=CASE WHEN ? THEN ? ELSE tokens END WHERE id=?",
                (
                    "complete" if known else "unknown",
                    known,
                    sum(values) if known else 0,
                    reservation,
                ),
            )

    async def decision(self, scope, reason):
        """Keep bounded decision counters without storing media references.

        Args:
            scope: Conversation identity.
            reason: Stable policy outcome.
        """
        async with self.store.transaction():
            await self.store.connection.execute(
                "INSERT INTO media_decisions(created_at,scope_type,scope_key,reason) VALUES(?,?,?,?)",
                (int(time.time()), *scope, reason),
            )
            await self.store.connection.execute(
                "DELETE FROM media_decisions WHERE id <= (SELECT MAX(id)-10000 FROM media_decisions)"
            )
