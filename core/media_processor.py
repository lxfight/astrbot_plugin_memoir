"""Bounded, durable media processing outside the response hook."""

import asyncio
import json
import time
from types import SimpleNamespace

from astrbot.api import logger
from astrbot.api.message_components import Image, Record

from .compatibility import native_policy
from .forward_parser import ForwardExpander, snapshot_forward
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
                "UPDATE work_items SET status='pending' WHERE kind IN ('media','forward') AND status='running'"
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
        forward = snapshot_forward(event)
        if forward:
            forward.update(
                triggered=bool(
                    getattr(event, "is_at_or_wake_command", False)
                    or scope.scope_type == "private"
                ),
                umo=event.unified_msg_origin,
                revision=await self.store.get_revision(
                    scope.scope_type, scope.scope_key
                ),
            )
            await self.store.connection.execute(
                "UPDATE raw_turns SET source_kind='forward_root',extracted=1,source_meta=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "status": "pending",
                            "platform": forward["platform"],
                            "platform_id": forward["platform_id"],
                            "event_id": forward["event_id"],
                        }
                    ),
                    raw_id,
                ),
            )
            job_id = await self.store.record_work(
                "forward", scope.scope_type, scope.scope_key, [raw_id], payload=forward
            )
            cursor = await self.store.connection.execute(
                "SELECT status,error FROM work_items WHERE id=?", (job_id,)
            )
            queued = await cursor.fetchone()
            if queued["status"] == "failed":
                await self.store.connection.execute(
                    "UPDATE raw_turns SET source_meta=? WHERE id=?",
                    (
                        json.dumps(
                            {
                                "status": "failed",
                                "problems": [queued["error"]],
                                "retryable": True,
                            }
                        ),
                        raw_id,
                    ),
                )
            self.wakeup.set()
            return
        parts = [
            p for p in event.message_obj.message or [] if isinstance(p, (Image, Record))
        ]
        if not parts:
            return
        main_request = bool(
            scope.scope_type == "private"
            or getattr(event, "is_at_or_wake_command", False)
            or event.get_extra("provider_request")
        )
        request = event.get_extra("provider_request")
        if request is not None and getattr(request, "conversation", None) is None:
            main_request = False
        selected_provider = event.get_extra("selected_provider")
        selected_provider = (
            selected_provider if isinstance(selected_provider, str) else ""
        )
        native = await native_policy(
            self.context,
            self.config,
            event.unified_msg_origin,
            scope.scope_type,
            check_request_image=main_request
            and any(isinstance(part, Image) for part in parts),
            provider_id=selected_provider,
            message_text=event.message_str or "",
        )
        parts = [
            part
            for part in parts
            if not (isinstance(part, Record) and native["audio"])
            and not (
                isinstance(part, Image)
                and (
                    native["group_image"] or (main_request and native["request_image"])
                )
            )
        ]
        if not parts:
            return
        error = ""
        refs = []
        size = 0
        for part in parts[:32]:
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
                    "main_request": main_request,
                    "selected_provider": selected_provider,
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
                "DELETE FROM work_items WHERE kind IN ('media','forward') AND NOT EXISTS(SELECT 1 FROM raw_turns WHERE id=work_items.raw_id)"
            )
            cursor = await store.connection.execute(
                "SELECT * FROM work_items WHERE kind IN ('media','forward') AND status='pending' ORDER BY id LIMIT 1"
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
        if job["kind"] == "forward":
            await self._process_forward(
                job, payload, effective, revision, config_revision
            )
            return True
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
                indices = {id(part): i for i, part in enumerate(parts, 1)}
                main_request = payload.get("main_request", scope_type == "private")
                native = await native_policy(
                    self.context,
                    effective,
                    payload.get("umo"),
                    scope_type,
                    check_request_image=main_request
                    and any(isinstance(part, Image) for part in parts),
                    provider_id=payload.get("selected_provider", ""),
                    message_text=payload.get("user_text", ""),
                )
                parts = [
                    part
                    for part in parts
                    if not (isinstance(part, Record) and native["audio"])
                    and not (
                        isinstance(part, Image)
                        and (
                            native["group_image"]
                            or (main_request and native["request_image"])
                        )
                    )
                ]
                event = SimpleNamespace(
                    message_obj=SimpleNamespace(message=parts),
                    message_str=payload["user_text"],
                    unified_msg_origin=payload["umo"],
                    memoir_indices={
                        i: indices[id(part)] for i, part in enumerate(parts, 1)
                    },
                    memoir_manual=payload.get("manual", False),
                    memoir_triggered=main_request,
                    memoir_selected=payload.get("selected", []),
                    memoir_completed=payload.setdefault("completed", {}),
                    memoir_completed_parts=payload.setdefault("completed_parts", {}),
                    memoir_job_id=job["id"],
                )
                description = await asyncio.wait_for(
                    describe_multimedia(
                        self.context,
                        effective,
                        event,
                        problems=problems,
                        store=store,
                        scope=(scope_type, scope_key),
                    ),
                    timeout=110,
                )
            except asyncio.TimeoutError:
                problems.append("Media processing exceeded the 110-second deadline")
            except Exception as exc:
                problems.append(f"Media processing failed: {type(exc).__name__}")
        notes = [p for p in problems if p.startswith(("cached:", "reused:"))]
        problems = [p for p in problems if p not in notes]
        policy_only = bool(problems) and all(
            p.startswith(("policy:", "budget:")) for p in problems
        )
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
                    (
                        "paused"
                        if any(p.startswith("budget:") for p in problems)
                        else "skipped"
                    )
                    if policy_only
                    else "failed"
                    if problems
                    else "complete",
                    "; ".join(problems + notes)[:300],
                    json.dumps(payload, ensure_ascii=False) if problems else "{}",
                    int(time.time()),
                    job["id"],
                ),
            )
        return True

    async def _process_forward(
        self, job, payload, effective, revision, config_revision
    ):
        """Expand quoted material outside transactions and atomically save chunks.

        Args:
            job: Claimed durable job.
            payload: Bounded adapter snapshot.
            effective: Effective scope settings at claim time.
            revision: Scope revision at claim time.
            config_revision: Global settings revision at claim time.
        """
        store = self.store
        scope_type, scope_key = job["scope_type"], job["scope_key"]
        result = ForwardExpander(self.context, payload)
        if (
            revision != payload["revision"]
            or not effective.get("scope_enabled", True)
            or not effective.get(f"enable_{scope_type}_memory", True)
        ):
            result.problems.append(
                "Conversation settings changed or memory is disabled"
            )
            result.retryable = True
        else:
            await result.expand()
            payload["available_parts"] = result.parts
            if result.parts:
                issues = []
                try:
                    parts = [
                        (Image if p["kind"] == "image" else Record)(
                            **{k: p[k] for k in ("file", "url", "path") if k in p}
                        )
                        for p in result.parts
                    ]
                    mapping = " / ".join(
                        f"媒体段{i}: 转发节点{p['node_path']}"
                        for i, p in enumerate(result.parts, 1)
                    )
                    event = SimpleNamespace(
                        message_obj=SimpleNamespace(message=parts),
                        message_str=mapping,
                        unified_msg_origin=payload["umo"],
                        memoir_forward=True,
                        memoir_manual=payload.get("manual", False),
                        memoir_triggered=payload.get(
                            "triggered", scope_type == "private"
                        ),
                        memoir_selected=payload.get("selected", []),
                        memoir_completed=payload.setdefault("completed", {}),
                        memoir_completed_parts=payload.setdefault(
                            "completed_parts", {}
                        ),
                        memoir_job_id=job["id"],
                    )
                    description = await asyncio.wait_for(
                        describe_multimedia(
                            self.context,
                            effective,
                            event,
                            problems=issues,
                            report_unsupported=True,
                            store=store,
                            scope=(scope_type, scope_key),
                        ),
                        110,
                    )
                    if description:
                        result.nodes.append(
                            {
                                "path": "media",
                                "text": f"[多媒体解析] {mapping} / {description}",
                            }
                        )
                    elif not issues:
                        result.problems.append(
                            "Selected model does not support the supplied media; placeholders retained"
                        )
                except Exception as exc:
                    issues.append(
                        f"Forward media processing failed: {type(exc).__name__}"
                    )
                notes = [i for i in issues if i.startswith(("cached:", "reused:"))]
                issues = [i for i in issues if i not in notes]
                result.problems.extend(issues)
                result.retryable |= any(
                    not issue.startswith("Unsupported media:") for issue in issues
                )
        async with store.transaction():
            cursor = await store.connection.execute(
                "SELECT r.* FROM raw_turns r JOIN work_items w ON w.raw_id=r.id WHERE w.id=?",
                (job["id"],),
            )
            source = await cursor.fetchone()
            if source is None:
                return
            if (
                revision != await store.get_revision(scope_type, scope_key)
                or config_revision != store.config_revision
            ):
                result.nodes = []
                result.problems.append(
                    "Settings changed during forward processing; retry"
                )
                result.retryable = True
            full_text = " ".join(n["text"] for n in result.nodes)
            if scope_type == "group" and (
                source["speaker_id"]
                in (effective.get("group_capture_ignored_users") or [])
                or any(
                    str(k).strip() in full_text
                    for k in effective.get("group_capture_ignored_keywords") or []
                    if str(k).strip()
                )
            ):
                await store.delete_raw_turn_in_scope(
                    source["id"], scope_type, scope_key
                )
                return
            cursor = await store.connection.execute(
                "SELECT id,source_meta FROM raw_turns WHERE parent_id=?",
                (source["id"],),
            )
            previous = [dict(r) for r in await cursor.fetchall()]
            status = (
                "partial"
                if result.problems and (result.nodes or previous)
                else "failed"
                if result.retryable
                else "unsupported"
                if result.problems
                else "complete"
            )
            metadata = {
                "status": status,
                "platform": payload["platform"],
                "platform_id": payload["platform_id"],
                "event_id": payload.get("event_id", ""),
                "problems": list(dict.fromkeys(result.problems))[:10],
                "retryable": result.retryable,
            }
            # Stable node/chunk identity makes retries idempotent and preserves source IDs.
            existing = {json.loads(r["source_meta"]).get("chunk") for r in previous}
            for row in previous:
                origin = json.loads(row["source_meta"])
                origin["status"] = status
                await store.connection.execute(
                    "UPDATE raw_turns SET source_meta=? WHERE id=?",
                    (json.dumps(origin, ensure_ascii=False), row["id"]),
                )
            for node in result.nodes:
                for offset in range(0, len(node["text"]), 1500):
                    chunk = f"{node['path']}:{offset // 1500}"
                    if chunk in existing:
                        if node["path"] == "media":
                            await store.connection.execute(
                                "UPDATE raw_turns SET content=?,extracted=0 WHERE parent_id=? AND json_extract(source_meta,'$.chunk')=?",
                                (
                                    f"[转发引用 #{source['id']} 节点{node['path']}，署名未验证] "
                                    + node["text"][offset : offset + 1500],
                                    source["id"],
                                    chunk,
                                ),
                            )
                        continue
                    origin = {k: v for k, v in node.items() if k != "text"}
                    origin.update(chunk=chunk, status=status)
                    label = f"[转发引用 #{source['id']} 节点{node['path']}，署名未验证：{node.get('name') or '?'} ({node.get('id') or '?'})，原时间：{node.get('time') or '?'}] "
                    await store.connection.execute(
                        "INSERT INTO raw_turns(scope_type,scope_key,content,extracted,created_at,parent_id,source_kind,source_meta) VALUES(?,?,?,0,?,?,'forwarded',?)",
                        (
                            scope_type,
                            scope_key,
                            " ".join(label.split())
                            + " "
                            + node["text"][offset : offset + 1500],
                            source["created_at"],
                            source["id"],
                            json.dumps(origin, ensure_ascii=False),
                        ),
                    )
            await store.connection.execute(
                "UPDATE raw_turns SET content=?,source_meta=? WHERE id=?",
                (
                    f"[转发消息：{status}；引用内容不代表转发者本人陈述] "
                    + "; ".join(metadata["problems"]),
                    json.dumps(metadata, ensure_ascii=False),
                    source["id"],
                ),
            )
            payload["retryable"] = result.retryable
            await store.connection.execute(
                "UPDATE work_items SET status=?,error=?,payload=?,updated_at=? WHERE id=?",
                (
                    (
                        "paused"
                        if any(p.startswith("budget:") for p in result.problems)
                        else "skipped"
                    )
                    if result.problems
                    and all(
                        p.startswith(("policy:", "budget:")) for p in result.problems
                    )
                    else "failed"
                    if result.problems
                    else "complete",
                    "; ".join(metadata["problems"])[:300],
                    json.dumps(payload, ensure_ascii=False)
                    if result.retryable
                    else json.dumps({"retryable": False}),
                    int(time.time()),
                    job["id"],
                ),
            )

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
