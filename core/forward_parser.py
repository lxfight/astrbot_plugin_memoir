"""Bounded inbound forwarding snapshots and adapter-specific expansion."""

import asyncio
import json
from html.parser import HTMLParser

MAX_DEPTH = 5
MAX_NODES = 100
MAX_TEXT = 20000
MAX_REFS = 4 * 1024 * 1024


class SatoriForwardParser(HTMLParser):
    """Preserve message and author boundaries from Satori's inline markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"type": "nodes", "nodes": []}
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        """Build bounded containers while preserving inline author metadata.

        Args:
            tag: Lowercase Satori element name.
            attrs: Parsed attribute pairs.

        Raises:
            ValueError: Markup exceeds the structural budget.
        """
        self.count += 1
        if self.count > 600 or len(self.stack) > 20:
            raise ValueError("Satori markup exceeds the structural budget")
        data = dict(attrs)
        parent = self.stack[-1]
        children = parent.setdefault("nodes" if parent is self.root else "content", [])
        if tag == "author":
            parent.update(name=data.get("name", ""), uin=data.get("id", ""))
            return
        if tag == "message":
            item = {
                "type": "nodes" if "forward" in data else "node",
                "content": [],
                "time": data.get("time", ""),
            }
            if "forward" in data and data.get("id"):
                item = {"type": "forward", "id": data["id"]}
        elif tag in {"img", "audio", "file", "video"}:
            children.append(
                {
                    "type": {"img": "image", "audio": "record"}.get(tag, tag),
                    "url": data.get("src", ""),
                }
            )
            return
        elif tag == "br":
            children.append({"type": "text", "text": " "})
            return
        else:
            item = {"type": "nodes", "content": []}
        children.append(item)
        self.stack.append(item)

    def handle_endtag(self, tag):
        """Close a container, excluding Satori's void elements.

        Args:
            tag: Closing element name.
        """
        if (
            tag not in {"author", "img", "audio", "file", "video", "br"}
            and len(self.stack) > 1
        ):
            self.stack.pop()

    def handle_data(self, data):
        """Attach visible text to its current message container.

        Args:
            data: Entity-decoded text.
        """
        parent = self.stack[-1]
        parent.setdefault("nodes" if parent is self.root else "content", []).append(
            {"type": "text", "text": data}
        )


def snapshot_forward(event):
    """Snapshot only bounded inbound fields without resolving media or networks.

    Args:
        event: Incoming AstrBot event, including optional native payload.

    Returns:
        Durable forwarding payload, or None for ordinary messages.
    """
    chain = getattr(event.message_obj, "message", None) or []
    raw = getattr(event.message_obj, "raw_message", None)
    platform = event.get_platform_name()
    problems = []
    native_forward = False
    if isinstance(raw, dict) and isinstance(raw.get("message"), dict):
        markup = raw["message"].get("content", "")
        if isinstance(markup, str) and "<message" in markup and "forward" in markup:
            native_forward = True
            parser = SatoriForwardParser()
            try:
                if len(markup) > MAX_REFS:
                    raise ValueError("Satori markup exceeds the snapshot budget")
                parser.feed(markup)
                chain = parser.root["nodes"]
            except ValueError as exc:
                problems.append(str(exc))
    # Telegram exposes Update.effective_message; Discord exposes Message snapshots.
    native = getattr(raw, "effective_message", raw)
    origin = getattr(native, "forward_origin", None)
    if origin:
        native_forward = True
        claimed = getattr(origin, "sender_user", None) or getattr(origin, "chat", None)
        chain = [
            {
                "type": "node",
                "name": getattr(origin, "sender_user_name", "")
                or getattr(claimed, "full_name", "")
                or getattr(claimed, "title", ""),
                "uin": getattr(claimed, "id", ""),
                "time": str(getattr(origin, "date", "")),
                "content": chain,
            }
        ]
        problems.append(
            "Only the delivered Telegram body is available; original chain unavailable"
        )
    snapshots = getattr(native, "message_snapshots", None)
    if snapshots:
        native_forward = True
        chain = list(chain)
        for snapshot in snapshots[:MAX_NODES]:
            content = [{"type": "text", "text": getattr(snapshot, "content", "")}]
            for attachment in (getattr(snapshot, "attachments", None) or [])[:5]:
                mime = getattr(attachment, "content_type", "") or ""
                kind = (
                    "image"
                    if mime.startswith("image/")
                    else "record"
                    if mime.startswith("audio/")
                    else "file"
                )
                content.append({"type": kind, "url": getattr(attachment, "url", "")})
            chain.append(
                {
                    "type": "node",
                    "time": str(getattr(snapshot, "created_at", "")),
                    "content": content,
                }
            )
        problems.append(
            "Only delivered Discord snapshots are available; original chain unavailable"
        )
    if (
        platform == "aiocqhttp"
        and isinstance(raw, dict)
        and isinstance(raw.get("message"), list)
    ):
        chain = raw["message"]

    if getattr(native, "message_type", None) == "merge_forward":
        native_forward = True
        problems.append(
            "Lark merge-forward body is not exposed by this adapter; preview only"
        )
    count, ref_size, text_size = 0, 0, 0
    active = set()
    found = native_forward

    def walk(value, depth=0):
        nonlocal count, ref_size, text_size, found
        if depth > MAX_DEPTH * 3 or count >= 600:
            problems.append("Snapshot structure truncated")
            return []
        if isinstance(value, str):
            if value.lstrip().startswith(("[", "{")) and len(value) <= MAX_REFS:
                try:
                    return walk(json.loads(value), depth + 1)
                except ValueError:
                    pass
            return walk({"type": "text", "text": value}, depth + 1)
        if isinstance(value, list):
            result = []
            for child in value[:600]:
                if count >= 600:
                    break
                result.extend(walk(child, depth + 1))
            if len(value) > 600:
                problems.append("Snapshot segment limit reached")
            return result
        if id(value) in active:
            problems.append("Inline cycle omitted")
            return []
        active.add(id(value))
        count += 1
        try:
            if isinstance(value, dict):
                kind = str(
                    value.get(
                        "type",
                        "node"
                        if "content" in value or "message" in value
                        else "unknown",
                    )
                ).lower()
                data = value.get("data", value)
            else:
                component_type = getattr(value, "type", "unknown")
                kind = str(getattr(component_type, "value", component_type)).lower()
                data = vars(value) if hasattr(value, "__dict__") else {}
            if not isinstance(data, dict):
                return []
            if kind in {"node", "nodes", "forward", "forward_msg"}:
                found = True
                sender = data.get("sender") or {}
                sender = sender if isinstance(sender, dict) else {}
                if kind in {"forward", "forward_msg"} and data.get("content"):
                    kind = "nodes"
                if kind in {"forward", "forward_msg"}:
                    return [{"type": "forward", "id": str(data.get("id", ""))[:256]}]
                return [
                    {
                        "type": kind,
                        "name": str(
                            data.get("name")
                            or data.get("nickname")
                            or sender.get("nickname")
                            or sender.get("card")
                            or ""
                        )[:100],
                        "uin": str(
                            data.get("uin")
                            or data.get("user_id")
                            or sender.get("user_id")
                            or ""
                        )[:100],
                        "time": str(data.get("time") or "")[:40],
                        "content": walk(
                            data.get(
                                "nodes", data.get("content", data.get("message", []))
                            ),
                            depth + 1,
                        ),
                    }
                ]
            if kind in {"plain", "text"}:
                text = str(data.get("text", ""))
                remaining = max(0, MAX_TEXT - text_size)
                if len(text) > remaining:
                    problems.append(
                        "Forward snapshot text truncated at 20000 characters"
                    )
                text = text[:remaining]
                text_size += len(text)
                return [{"type": "text", "text": text}]
            if kind in {"image", "record", "audio"}:
                item = {"type": "record" if kind == "audio" else kind}
                for key in ("file", "url", "path"):
                    ref = data.get(key)
                    if isinstance(ref, str):
                        ref_size += len(ref.encode("utf-8"))
                        if ref_size <= MAX_REFS:
                            item[key] = ref
                if ref_size > MAX_REFS:
                    problems.append("Attachment references exceed 4 MiB")
                return [item]
            if kind == "json":
                card = data.get("data", "")
                try:
                    card = (
                        json.loads(card)
                        if isinstance(card, str) and len(card) <= MAX_REFS
                        else card
                    )
                    if (
                        isinstance(card, dict)
                        and card.get("app") == "com.tencent.multimsg"
                    ):
                        found = True
                        news = card.get("meta", {}).get("detail", {}).get("news", [])
                        preview = " / ".join(
                            str(n.get("text", ""))
                            for n in news[:100]
                            if isinstance(n, dict)
                        )
                        problems.append("Forward card preview only; body unavailable")
                        return [{"type": "text", "text": preview[:MAX_TEXT]}]
                except (ValueError, TypeError, AttributeError):
                    pass
            return [{"type": "text", "text": f"[{kind[:30]}]"}]
        finally:
            active.remove(id(value))

    segments = walk(chain)
    if not found:
        return None
    # A final byte cap also covers accumulated text from many nodes.
    if len(json.dumps(segments, ensure_ascii=False).encode("utf-8")) > MAX_REFS:
        segments = []
        problems.append("Forward snapshot exceeds 4 MiB")
    return {
        "segments": segments,
        "platform": platform,
        "platform_id": event.get_platform_id(),
        "event_id": str(getattr(event.message_obj, "message_id", ""))[:256],
        "problems": list(dict.fromkeys(problems))[:10],
    }


class ForwardExpander:
    """Expand references with shared depth, node, action, time and media budgets."""

    def __init__(self, context, payload):
        self.context, self.payload = context, payload
        self.nodes, self.parts, self.problems = (
            [],
            [],
            list(payload.get("problems", [])),
        )
        self.seen = set()
        self.actions = self.chars = self.segments = self.ref_bytes = 0
        self.retryable = False
        self.call_action = None

    async def expand(self):
        """Resolve the exact adapter instance and expand within thirty seconds.

        Returns:
            This expander with partial results retained on timeout or failure.
        """
        try:
            if self.payload["platform"] == "aiocqhttp":
                manager = getattr(self.context, "platform_manager", None)
                for inst in manager.get_insts() if manager else []:
                    if (
                        inst.meta().id == self.payload["platform_id"]
                        and inst.meta().name == "aiocqhttp"
                    ):
                        bot = inst.get_client()
                        self.call_action = getattr(bot, "call_action", None) or getattr(
                            getattr(bot, "api", None), "call_action", None
                        )
                        break
            await asyncio.wait_for(self._walk(self.payload["segments"], (), {}, 0), 30)
        except asyncio.TimeoutError:
            self.problems.append("Forward expansion exceeded 30 seconds")
            self.retryable = True
        except Exception as exc:
            self.problems.append(f"Forward expansion failed: {type(exc).__name__}")
            self.retryable = True
        if not self.nodes and not self.problems:
            self.problems.append("No readable forwarded content was delivered")
        self.problems = list(dict.fromkeys(self.problems))[:10]
        return self

    async def _walk(self, segments, path, author, depth):
        """Walk inline segments and lazily fetch OneBot references in order.

        Args:
            segments: Normalized segments or OneBot response components.
            path: Stable parent path.
            author: Claimed author fields, never authenticated sender identity.
            depth: Current forwarding depth.
        """
        if depth > MAX_DEPTH:
            self.problems.append("Maximum forward depth (5) reached")
            return
        if isinstance(segments, str):
            if len(segments) > MAX_REFS:
                self.problems.append("Remote forward body exceeds 4 MiB")
                return
            try:
                segments = json.loads(segments)
            except ValueError:
                segments = [{"type": "text", "text": segments}]
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, list):
            self.problems.append("Unsupported forward body")
            return
        for index, segment in enumerate(segments):
            if (
                len(self.nodes) >= MAX_NODES
                or self.chars >= MAX_TEXT
                or self.segments >= 600
            ):
                self.problems.append("Forward node, segment or text budget reached")
                return
            self.segments += 1
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type", "node")).lower()
            data = segment.get("data", segment)
            if not isinstance(data, dict):
                continue
            current = path + (index + 1,)
            if kind in {"forward", "forward_msg"} and data.get("content"):
                kind = "nodes"
            if kind in {"node", "nodes"}:
                sender = data.get("sender") or {}
                sender = sender if isinstance(sender, dict) else {}
                who = {
                    "name": str(
                        data.get("name")
                        or data.get("nickname")
                        or sender.get("nickname")
                        or sender.get("card")
                        or ""
                    )[:100],
                    "id": str(
                        data.get("uin")
                        or data.get("user_id")
                        or sender.get("user_id")
                        or ""
                    )[:100],
                    "time": str(data.get("time") or "")[:40],
                }
                await self._walk(
                    data.get("nodes", data.get("content", data.get("message", []))),
                    current,
                    who if kind == "node" else author,
                    depth + (kind == "node"),
                )
                continue
            if kind in {"forward", "forward_msg"}:
                ref = str(data.get("id", ""))[:256]
                if ref in self.seen:
                    self.problems.append("Repeated or cyclic forward reference omitted")
                    continue
                self.seen.add(ref)
                if not ref or not callable(self.call_action):
                    self.problems.append(
                        "Forward reference unavailable on this adapter instance"
                    )
                    self.retryable |= self.payload["platform"] == "aiocqhttp" and bool(
                        ref
                    )
                    continue
                body = None
                for value in [ref] + (
                    [int(ref)] if ref.isascii() and ref.isdigit() else []
                ):
                    for param in ("message_id", "id"):
                        if self.actions >= 10:
                            break
                        self.actions += 1
                        try:
                            response = await asyncio.wait_for(
                                self.call_action("get_forward_msg", **{param: value}), 5
                            )
                            response = (
                                response.get("data", response)
                                if isinstance(response, dict)
                                else response
                            )
                            if isinstance(response, dict):
                                body = next(
                                    (
                                        response[k]
                                        for k in (
                                            "messages",
                                            "message",
                                            "nodes",
                                            "nodeList",
                                        )
                                        if isinstance(response.get(k), (list, str))
                                    ),
                                    None,
                                )
                            elif isinstance(response, list):
                                body = response
                            if body:
                                break
                        except Exception:
                            pass
                    if body:
                        break
                if body:
                    await self._walk(body, current, author, depth)
                else:
                    self.problems.append(
                        "Forward fetch failed or action budget (10) exhausted"
                    )
                    self.retryable = True
                continue
            if kind == "json":
                from types import SimpleNamespace

                proxy = SimpleNamespace(
                    message_obj=SimpleNamespace(message=[segment]),
                    get_platform_name=lambda: self.payload["platform"],
                    get_platform_id=lambda: self.payload["platform_id"],
                )
                preview = snapshot_forward(proxy)
                if preview:
                    self.problems.extend(preview["problems"])
                    await self._walk(preview["segments"], current, author, depth)
                    continue
            text = ""
            if kind in {"plain", "text"}:
                text = str(data.get("text", ""))
            elif kind in {"image", "record", "audio"}:
                label = "图片" if kind == "image" else "语音"
                text = f"[{label}]"
                if len(self.parts) < 4:
                    part = {
                        "kind": "image" if kind == "image" else "audio",
                        "node_path": ".".join(map(str, current)),
                    }
                    for key in ("file", "url", "path"):
                        ref = data.get(key)
                        if isinstance(ref, str):
                            self.ref_bytes += len(ref.encode("utf-8"))
                            if self.ref_bytes <= MAX_REFS:
                                part[key] = ref
                    if self.ref_bytes <= MAX_REFS:
                        self.parts.append(part)
                        text = f"[{label}，媒体段{len(self.parts)}]"
                    else:
                        self.problems.append("Attachment references exceed 4 MiB")
                else:
                    self.problems.append(
                        "Only the first four forwarded attachments are analyzed"
                    )
            else:
                text = f"[{kind[:30]}：未解析]"
                self.problems.append("Forward contains unsupported components")
            text = " ".join(text.split())
            if text:
                if len(text) > MAX_TEXT - self.chars:
                    self.problems.append("Forward text truncated at 20000 characters")
                text = text[: MAX_TEXT - self.chars]
                self.chars += len(text)
                self.nodes.append(
                    {"path": ".".join(map(str, current)), **author, "text": text}
                )
