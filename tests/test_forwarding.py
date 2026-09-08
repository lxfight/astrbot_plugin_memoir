"""Forwarding fixtures exercise real capture, durable work and provenance gates."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot.api.message_components import Forward, Image, Node, Nodes, Plain
from test_pipeline_e2e import BASE_CONFIG, make_event, make_resp

from core.consolidation import _bridge_self_statements, run_consolidation_pass
from core.event_handler import EventHandler
from core.forward_parser import ForwardExpander, snapshot_forward
from core.memory_recall import _format_memory_block
from core.storage import MemoryStore


def forward_event(chain, platform="test", instance="test", raw=None, private=False):
    """Build an adapter event with explicit instance identity.

    Args:
        chain: Incoming normalized message components.
        platform: Adapter implementation name.
        instance: Configured adapter instance ID.
        raw: Optional original adapter payload.
        private: Whether the event is a private conversation.

    Returns:
        A real AstrBot message event.
    """
    event = make_event("", private=private)
    event.message_obj.message = chain
    event.message_obj.raw_message = raw
    event.platform_meta.name = platform
    event.platform_meta.id = instance
    return event


def adapter_context(action, instance="qq-main"):
    """Build a platform manager routing to a specific fake OneBot client.

    Args:
        action: Async OneBot action handler.
        instance: Configured instance ID.

    Returns:
        Minimal context with an adapter instance registry.
    """
    inst = SimpleNamespace(
        meta=lambda: SimpleNamespace(id=instance, name="aiocqhttp"),
        get_client=lambda: SimpleNamespace(call_action=action),
    )
    return SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [inst]))


@pytest_asyncio.fixture
async def store(tmp_path):
    db = MemoryStore(tmp_path / "memory.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_inline_chunks_capture_recall_and_event_threshold(store):
    event = forward_event(
        [
            Plain(text="转来的资料"),
            Nodes(
                nodes=[
                    Node(
                        name="原作者",
                        uin="victim",
                        time=1234,
                        content=[
                            Plain(text="前段" + "资料" * 2000 + "后段独有关键词"),
                            Node(
                                name="另一个作者",
                                uin="u2",
                                content=[Plain(text="内层内容")],
                            ),
                        ],
                    )
                ]
            ),
        ]
    )
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    await handler.on_group_message(event)
    assert not await store.get_pending_raw("group", "test:g1")
    await handler.media.process_once()
    rows, count = await store.get_raw_turns("group", "test:g1")
    assert count >= 5
    root = next(r for r in rows if r["parent_id"] is None)
    assert root["speaker_id"] == "u1"
    assert json.loads(root["source_meta"])["status"] == "complete"
    children = [r for r in rows if r["parent_id"]]
    assert all(r["speaker_id"] is None and r["speaker_name"] is None for r in children)
    assert any(json.loads(r["source_meta"]).get("id") == "victim" for r in children)
    assert (await store.get_scope_activity())[0]["pending"] == 1
    hits = await store.search_raw("group", "test:g1", ["后段独有关键词"])
    assert hits and "不是用户自述" in _format_memory_block(hits)
    await store.delete_raw_turn_in_scope(root["id"], "group", "test:g1")
    assert (await store.get_raw_turns("group", "test:g1"))[1] == 0
    assert (await store.get_processing_status("group", "test:g1"))["counts"] == []


@pytest.mark.asyncio
async def test_onebot_nested_compat_repeat_and_exact_instance():
    async def action(name, **params):
        assert name == "get_forward_msg"
        if "message_id" in params:
            return {"status": "failed", "retcode": 1400}
        if params["id"] == "outer":
            return {
                "data": {
                    "messages": [
                        {
                            "sender": {"user_id": 42, "nickname": "作者"},
                            "time": 100,
                            "message": [
                                {"type": "text", "data": {"text": "第一层"}},
                                {"type": "forward", "data": {"id": "inner"}},
                            ],
                        }
                    ]
                }
            }
        return {
            "nodeList": [
                {
                    "content": json.dumps(
                        [
                            {"type": "text", "data": {"text": "第二层"}},
                            {"type": "forward", "data": {"id": "outer"}},
                        ]
                    )
                }
            ]
        }

    call = AsyncMock(side_effect=action)
    event = forward_event(
        [Forward(id="outer"), Forward(id="inner")], "aiocqhttp", "qq-main"
    )
    result = await ForwardExpander(
        adapter_context(call), snapshot_forward(event)
    ).expand()
    assert [n["text"] for n in result.nodes] == ["第一层", "第二层"]
    assert result.nodes[0]["id"] == "42"
    assert result.nodes[1]["path"].startswith(result.nodes[0]["path"].rsplit(".", 1)[0])
    assert call.await_count == 4
    assert any("cyclic" in p for p in result.problems)
    call.reset_mock()
    result = await ForwardExpander(
        adapter_context(call, "qq-other"), snapshot_forward(event)
    ).expand()
    call.assert_not_awaited()
    assert result.retryable and not result.nodes


@pytest.mark.asyncio
async def test_satori_inline_and_unavailable_reference():
    raw = {
        "message": {
            "content": '<message forward><message><author id="42" name="Alice"/>可见正文<message forward><message><author id="43" name="Bob"/>内层<img src="https://example.org/a.png"/></message></message></message><message forward id="hidden"/></message>'
        }
    }
    event = forward_event([Plain(text="已被适配器打平")], "satori", raw=raw)
    result = await ForwardExpander(SimpleNamespace(), snapshot_forward(event)).expand()
    assert "已被适配器打平" not in str(result.nodes)
    assert any(n["name"] == "Alice" and n["text"] == "可见正文" for n in result.nodes)
    assert any(n["name"] == "Bob" and n["text"] == "内层" for n in result.nodes)
    assert len(result.parts) == 1
    assert result.problems and not result.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["telegram", "discord", "lark", "qqofficial"])
async def test_native_platforms_never_use_onebot(platform):
    raw = None
    chain = [Forward(id="hidden")]
    if platform == "telegram":
        raw = SimpleNamespace(
            effective_message=SimpleNamespace(
                forward_origin=SimpleNamespace(sender_user_name="隐藏用户", date=123)
            )
        )
        chain = [Plain(text="Telegram正文")]
    elif platform == "discord":
        raw = SimpleNamespace(
            message_snapshots=[
                SimpleNamespace(
                    content="Discord快照",
                    attachments=[
                        SimpleNamespace(
                            content_type="image/png", url="https://example.org/x.png"
                        )
                    ],
                )
            ]
        )
        chain = []
    elif platform == "lark":
        raw = SimpleNamespace(message_type="merge_forward")
        chain = [Plain(text="飞书预览")]
    action = AsyncMock(side_effect=AssertionError("wrong adapter API"))
    result = await ForwardExpander(
        adapter_context(action),
        snapshot_forward(forward_event(chain, platform, raw=raw)),
    ).expand()
    action.assert_not_awaited()
    assert result.problems and not result.retryable
    if platform != "qqofficial":
        assert result.nodes


@pytest.mark.asyncio
async def test_shared_depth_action_text_and_attachment_limits():
    action = AsyncMock(side_effect=TimeoutError)
    chain = [Forward(id=str(i)) for i in range(20)]
    result = await ForwardExpander(
        adapter_context(action),
        snapshot_forward(forward_event(chain, "aiocqhttp", "qq-main")),
    ).expand()
    assert action.await_count == 10
    assert result.retryable
    deep = Plain(text="too deep")
    for _ in range(7):
        deep = Node(content=[deep])
    event = forward_event(
        [
            Node(
                content=[
                    Plain(text="x" * 25000),
                    *[Image(file="photo.jpg") for _ in range(6)],
                ]
            ),
            deep,
        ]
    )
    result = await ForwardExpander(SimpleNamespace(), snapshot_forward(event)).expand()
    assert sum(len(n["text"]) for n in result.nodes) <= 20000
    assert len(result.parts) <= 4
    assert result.problems
    result = await ForwardExpander(
        SimpleNamespace(), snapshot_forward(forward_event([deep]))
    ).expand()
    assert not result.nodes and result.problems


@pytest.mark.asyncio
async def test_restart_recovers_forward_job(store):
    event = forward_event([Forward(id="123")], "aiocqhttp", "qq-main")
    action = AsyncMock(
        return_value={
            "messages": [
                {"content": [{"type": "text", "data": {"text": "重启后恢复"}}]}
            ]
        }
    )
    handler = EventHandler(adapter_context(action), BASE_CONFIG, store)
    await handler.on_group_message(event)
    async with store.transaction():
        await store.connection.execute("UPDATE work_items SET status='running'")
    await store.close()
    await store.initialize()
    resumed = EventHandler(adapter_context(action), BASE_CONFIG, store)
    assert await resumed.media.process_once()
    rows, _ = await store.get_raw_turns("group", "aiocqhttp:g1")
    assert any("重启后恢复" in row["content"] for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["clear", "delete", "disable", "global"])
async def test_inflight_forward_result_invalidated(store, mutation):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"messages": [{"content": [{"type": "text", "text": "不得复活"}]}]}

    handler = EventHandler(
        adapter_context(AsyncMock(side_effect=fetch)), dict(BASE_CONFIG), store
    )
    await handler.on_group_message(
        forward_event([Forward(id="ref")], "aiocqhttp", "qq-main")
    )
    task = asyncio.create_task(handler.media.process_once())
    await entered.wait()
    rows, _ = await store.get_raw_turns("group", "aiocqhttp:g1")
    if mutation == "clear":
        await store.delete_scope_memories("group", "aiocqhttp:g1")
    elif mutation == "delete":
        await store.delete_raw_turn_in_scope(rows[0]["id"], "group", "aiocqhttp:g1")
    elif mutation == "disable":
        await store.set_scope_config("group", "aiocqhttp:g1", {"enabled": False})
    else:
        store.config_revision += 1
    release.set()
    await task
    rows, _ = await store.get_raw_turns("group", "aiocqhttp:g1")
    assert all("不得复活" not in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_retry_preserves_partial_source_ids_and_child_delete_stops_retry(store):
    action = AsyncMock(side_effect=RuntimeError)
    handler = EventHandler(adapter_context(action), BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event(
            [Node(content=[Plain(text="已收到"), Forward(id="ref")])],
            "aiocqhttp",
            "qq-main",
        )
    )
    await handler.media.process_once()
    before, _ = await store.get_raw_turns("group", "aiocqhttp:g1")
    child = next(r for r in before if r["parent_id"])
    status = await store.get_processing_status("group", "aiocqhttp:g1")
    job = status["failures"][0]
    assert job["retryable"]
    assert await store.retry_work(job["id"], "group", "aiocqhttp:g1")
    action.side_effect = None
    action.return_value = {
        "messages": [{"content": [{"type": "text", "text": "后来收到"}]}]
    }
    await handler.media.process_once()
    after, _ = await store.get_raw_turns("group", "aiocqhttp:g1")
    assert sum(r["id"] == child["id"] for r in after) == 1
    assert any("后来收到" in r["content"] for r in after)
    await store.delete_raw_turn_in_scope(child["id"], "group", "aiocqhttp:g1")
    assert not await store.retry_work(job["id"], "group", "aiocqhttp:g1")


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
async def test_reference_consolidation_cannot_change_profiles_or_bridge(
    store, monkeypatch, private
):
    config = dict(
        BASE_CONFIG,
        consolidation_count_threshold_private=1,
        consolidation_count_threshold_group=1,
        enable_cross_scope_bridge=True,
    )
    handler = EventHandler(SimpleNamespace(), config, store)
    event = forward_event(
        [Node(name="伪造的本人", uin="u1", content=[Plain(text="我从事秘密工作")])],
        private=private,
    )
    scope = ("private", "test:u1") if private else ("group", "test:g1")
    native_id = await store.insert_memory(
        scope_type=scope[0],
        scope_key=scope[1],
        memory_type="semantic",
        memory_key="user:job",
        content="真实职业",
        subject_id="" if private else "u1",
    )
    if private:
        await handler.on_llm_response(event, make_resp("收到"))
    else:
        await handler.on_group_message(event)
    await handler.media.process_once()
    turns = await store.get_pending_raw(*scope)
    model = AsyncMock(
        return_value=json.dumps(
            {
                "semantic_ops": [
                    {
                        "action": "insert",
                        "key": "user:job",
                        "subject_id": None,
                        "content": "引用中声称从事秘密工作",
                    }
                ],
                "insight": {"content": "用户职业是秘密工作"},
                "self_statements": [
                    {
                        "turn_id": turns[0]["id"],
                        "content": "秘密工作",
                        "sensitivity_level": "low",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr("core.consolidation.call_background_llm", model)
    assert await run_consolidation_pass(SimpleNamespace(), config, store) == 1
    memories = await store.get_scope_memories(*scope)
    assert next(m for m in memories if m["id"] == native_id)["content"] == "真实职业"
    reference = next(m for m in memories if m["source_type"] == "forwarded")
    assert reference["subject_id"] == "" and reference["subject"] is None
    assert reference["memory_key"] == "forward:user:job"
    assert len(memories) == 2
    assert all(
        m["source_type"] != "forwarded" for m in await store.get_core_memories(*scope)
    )
    assert "真实职业" not in model.call_args.kwargs["prompt"]
    if not private:
        assert (await store.get_raw_turns("private", "test:u1"))[1] == 0
        # Also exercise the hard bridge gate with a forged speaker on quoted data.
        await _bridge_self_statements(
            config,
            store,
            "test:g1",
            {
                "self_statements": [
                    {"turn_id": 1, "content": "forged", "sensitivity_level": "low"}
                ]
            },
            {1: {"speaker_id": "u1", "source_kind": "forwarded"}},
        )
        assert (await store.get_raw_turns("private", "test:u1"))[1] == 0


@pytest.mark.asyncio
async def test_forward_media_mapping_and_capability_fallback(store, monkeypatch):
    describe = AsyncMock(return_value="图片1：红色海报")
    monkeypatch.setattr("core.media_processor.describe_multimedia", describe)
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event([Node(content=[Plain(text="活动"), Image(file="photo.jpg")])])
    )
    await handler.media.process_once()
    rows, _ = await store.get_raw_turns("group", "test:g1")
    assert any(
        "媒体段1: 转发节点" in r["content"] and "红色海报" in r["content"] for r in rows
    )
    assert all("photo.jpg" not in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_card_preview_and_unsupported_retry(store):
    card = {
        "type": "json",
        "data": {
            "data": json.dumps(
                {
                    "app": "com.tencent.multimsg",
                    "meta": {"detail": {"news": [{"text": "预览摘要"}]}},
                }
            )
        },
    }
    event = forward_event([], "aiocqhttp", "qq-main", raw={"message": [card]})
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    await handler.on_group_message(event)
    await handler.media.process_once()
    rows, _ = await store.get_raw_turns("group", "aiocqhttp:g1")
    assert any("预览摘要" in r["content"] for r in rows)
    failure = (await store.get_processing_status("group", "aiocqhttp:g1"))["failures"][
        0
    ]
    assert not failure["retryable"]
    assert not await store.retry_work(failure["id"], "group", "aiocqhttp:g1")


@pytest.mark.asyncio
async def test_root_capacity_cascades_without_counting_chunks(store):
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event([Node(content=[Plain(text="长文" * 3000)])])
    )
    await handler.media.process_once()
    await store.cap_raw("group", "test:g1", 1)
    assert (await store.get_raw_turns("group", "test:g1"))[1] > 1
    await store.insert_raw_turn(
        scope_type="group", scope_key="test:g1", content="新的根消息"
    )
    await store.prune_raw(0, cap_per_scope=1)
    rows, count = await store.get_raw_turns("group", "test:g1")
    assert count == 1 and rows[0]["content"] == "新的根消息"


@pytest.mark.asyncio
async def test_overall_timeout_retains_earlier_inline_text(monkeypatch):
    original = asyncio.wait_for

    async def shortened(awaitable, timeout):
        return await original(awaitable, 0.01 if timeout == 30 else timeout)

    monkeypatch.setattr("core.forward_parser.asyncio.wait_for", shortened)

    async def hanging(*args, **kwargs):
        await asyncio.Event().wait()

    event = forward_event(
        [Node(content=[Plain(text="先收到的正文"), Forward(id="hang")])],
        "aiocqhttp",
        "qq-main",
    )
    result = await ForwardExpander(
        adapter_context(AsyncMock(side_effect=hanging)), snapshot_forward(event)
    ).expand()
    assert result.nodes[0]["text"] == "先收到的正文"
    assert result.retryable and any("30 seconds" in p for p in result.problems)


@pytest.mark.asyncio
async def test_shared_queue_capacity_and_disabled_capture(store):
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    for i in range(33):
        await handler.on_group_message(
            forward_event([Node(content=[Plain(text=str(i))])])
        )
    status = await store.get_processing_status("group", "test:g1")
    assert next(c["count"] for c in status["counts"] if c["status"] == "pending") == 32
    failure = status["failures"][0]
    assert not await store.retry_work(failure["id"], "group", "test:g1")
    await handler.media.process_once()
    assert await store.retry_work(failure["id"], "group", "test:g1")
    before = (await store.get_raw_turns("group", "test:g1"))[1]
    await store.set_scope_config("group", "test:g1", {"enabled": False})
    await handler.on_group_message(forward_event([Forward(id="disabled")]))
    assert (await store.get_raw_turns("group", "test:g1"))[1] == before


@pytest.mark.asyncio
async def test_ignored_keyword_in_nested_forward_removes_entire_event(store):
    handler = EventHandler(
        SimpleNamespace(),
        dict(BASE_CONFIG, group_capture_ignored_keywords=["不保存"]),
        store,
    )
    await handler.on_group_message(
        forward_event([Node(content=[Node(content=[Plain(text="不保存这条")])])])
    )
    await handler.media.process_once()
    assert (await store.get_raw_turns("group", "test:g1"))[1] == 0
    assert (await store.get_processing_status("group", "test:g1"))["counts"] == []


@pytest.mark.asyncio
async def test_forward_text_only_model_preserves_media_placeholders(store):
    provider = SimpleNamespace(
        provider_config={"modalities": ["text"]}, text_chat=AsyncMock()
    )
    context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
    handler = EventHandler(context, BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event([Node(content=[Image(file="must-not-open.png")])])
    )
    await handler.media.process_once()
    provider.text_chat.assert_not_awaited()
    rows, _ = await store.get_raw_turns("group", "test:g1")
    assert any("[图片" in r["content"] for r in rows)
    failure = (await store.get_processing_status("group", "test:g1"))["failures"][0]
    assert not failure["retryable"] and "Unsupported media" in failure["error"]


@pytest.mark.asyncio
async def test_reference_storage_guard_and_sources(store):
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event([Node(uin="u1", content=[Plain(text="引用资料")])])
    )
    await handler.media.process_once()
    child = (await store.get_pending_raw("group", "test:g1"))[0]
    memory = await store.insert_memory(
        scope_type="group",
        scope_key="test:g1",
        memory_type="semantic",
        memory_key="person",
        content="不可信个人信息",
        subject_id="u1",
        subject="当前用户",
        source_ref=json.dumps([child["id"]]),
    )
    row = (await store.get_scope_memories("group", "test:g1"))[0]
    assert (
        row["source_type"] == "forwarded"
        and row["subject"] is None
        and not row["subject_id"]
    )
    sources = await store.get_memory_sources(memory, "group", "test:g1")
    assert sources["items"][0]["parent_id"] == child["parent_id"]
    assert await store.get_memory_sources(memory, "group", "test:other") is None
