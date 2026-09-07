"""Bounded, durable media processing outside the response hook."""

import asyncio
import json
import time
from types import SimpleNamespace

from astrbot.api import logger
from astrbot.api.message_components import Image, Record

from .llm_helper import describe_multimedia
from .scope import merge_scope_config


class MediaProcessor:
    """Run two workers over a durable queue capped at 32 active jobs."""

    def __init__(self, context, config, store):
        self.context, self.config, self.store = context, config, store
        self.tasks = []
        self.wakeup = asyncio.Event()

    def start(self):
        """Start workers after storage initialization."""
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._run()) for _ in range(2)]

    async def stop(self):
        """Cancel workers before closing the shared database."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        async with self.store.transaction():
            await self.store.connection.execute(
                "UPDATE work_items SET status='pending' WHERE kind='media' AND status='running'"
            )

    async def enqueue(self, event, scope, raw_id, user_text, assistant_text=""):
        """Save bounded attachment references for background processing/retry.

        Args:
            event: Incoming AstrBot event.
            scope: Resolved conversation identity.
            raw_id: Already persisted source turn.
            user_text: Original text with media markers.
            assistant_text: Assistant response for private turns.
        """
        parts = [
            p for p in event.message_obj.message or [] if isinstance(p, (Image, Record))
        ]
        if not parts:
            return
        error = ""
        if len(parts) > 4:
            error = "At most four attachments can be analyzed per message; resend fewer attachments"
            parts = []
        refs = []
        size = 0
        for part in parts:
            item = {"kind": "image" if isinstance(part, Image) else "audio"}
            for key in ("file", "url", "path"):
                item[key] = getattr(part, key, None)
                size += len(item[key] or "")
            if size > 4 * 1024 * 1024:
                refs = []
                error = (
                    "Encoded attachment references exceed 4 MiB; resend smaller media"
                )
                break
            refs.append(item)
        async with self.store.transaction():
            await self.store.record_work(
                "media",
                scope.scope_type,
                scope.scope_key,
                [raw_id],
                payload={
                    "parts": refs,
                    "user_text": user_text[:2000],
                    "assistant_text": assistant_text[:2000],
                    "umo": event.unified_msg_origin,
                    "revision": await self.store.get_revision(
                        scope.scope_type, scope.scope_key
                    ),
                },
                error=error,
            )
        self.wakeup.set()

    async def process_once(self):
        """Claim one job and apply its result only if its source is still valid.

        Returns:
            Whether a queued job was found.
        """
        store = self.store
        async with store.transaction():
            await store.connection.execute(
                "DELETE FROM work_items WHERE kind='media' AND NOT EXISTS(SELECT 1 FROM raw_turns WHERE id=work_items.raw_id)"
            )
            cursor = await store.connection.execute(
                "SELECT * FROM work_items WHERE kind='media' AND status='pending' ORDER BY id LIMIT 1"
            )
            row = await cursor.fetchone()
            if not row:
                return False
            job = dict(row)
            payload = json.loads(job["payload"])
            scope_type, scope_key = job["scope_type"], job["scope_key"]
            effective = merge_scope_config(
                self.config,
                await store.get_scope_config(scope_type, scope_key),
                scope_type,
            )
            config_revision = store.config_revision
            revision = await store.get_revision(scope_type, scope_key)
            await store.connection.execute(
                "UPDATE work_items SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (int(time.time()), job["id"]),
            )
        problems = []
        description = ""
        if (
            revision != payload["revision"]
            or not effective.get("scope_enabled", True)
            or not effective.get(f"enable_{scope_type}_memory", True)
        ):
            problems.append("Conversation settings changed or memory is disabled")
        elif not payload["parts"]:
            problems.append("Original attachments unavailable; resend the media")
        else:
            try:
                parts = [
                    (Image if p["kind"] == "image" else Record)(
                        file=p.get("file"), url=p.get("url"), path=p.get("path")
                    )
                    for p in payload["parts"]
                ]
                event = SimpleNamespace(
                    message_obj=SimpleNamespace(message=parts),
                    message_str=payload["user_text"],
                    unified_msg_origin=payload["umo"],
                )
                description = await asyncio.wait_for(
                    describe_multimedia(
                        self.context, effective, event, problems=problems
                    ),
                    timeout=45,
                )
            except asyncio.TimeoutError:
                problems.append("Media processing exceeded the 45-second deadline")
            except Exception as exc:
                problems.append(f"Media processing failed: {type(exc).__name__}")
        async with store.transaction():
            cursor = await store.connection.execute(
                "SELECT id FROM work_items WHERE id=?", (job["id"],)
            )
            if not await cursor.fetchone():
                return True
            cursor = await store.connection.execute(
                "SELECT speaker_id FROM raw_turns WHERE id=?", (job["raw_id"],)
            )
            source = await cursor.fetchone()
            if source is None:
                await store.connection.execute(
                    "DELETE FROM work_items WHERE id=?", (job["id"],)
                )
                return True
            if (
                revision != await store.get_revision(scope_type, scope_key)
                or config_revision != store.config_revision
            ):
                description = ""
                problems.append("Settings changed while media was processing; retry")
            if scope_type == "group":
                ignored = effective.get("group_capture_ignored_keywords") or []
                if any(
                    str(k).strip() in description for k in ignored if str(k).strip()
                ) or source[0] in (effective.get("group_capture_ignored_users") or []):
                    await store.delete_raw_turn_in_scope(
                        job["raw_id"], scope_type, scope_key
                    )
                    await store.connection.execute(
                        "DELETE FROM work_items WHERE id=?", (job["id"],)
                    )
                    return True
            if description:
                text = f"{payload['user_text'][:1000]} [多媒体解析] {description}"
                if scope_type == "private":
                    text = f"用户: {text} / 助手: {payload['assistant_text']}"
                await store.connection.execute(
                    "UPDATE raw_turns SET content=?,extracted=0 WHERE id=?",
                    (text[:2000], job["raw_id"]),
                )
            await store.connection.execute(
                "UPDATE work_items SET status=?,error=?,payload=?,updated_at=? WHERE id=?",
                (
                    "failed" if problems else "complete",
                    "; ".join(problems)[:300],
                    job["payload"] if problems else "{}",
                    int(time.time()),
                    job["id"],
                ),
            )
        return True

    async def _run(self):
        """Poll the persisted queue, waking immediately after new capture."""
        while True:
            try:
                self.wakeup.clear()
                if await self.process_once():
                    continue
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[Memoir] Media worker failed (%s)", type(exc).__name__)
                await asyncio.sleep(1)
