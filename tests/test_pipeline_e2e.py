"""Pipeline end-to-end tests: drive the real memoir memory loop.

Unlike test_memory.py (storage/op level units), these tests run the actual
hook pipeline with genuine AstrBot objects:

    AstrMessageEvent -> EventHandler (recall/capture hooks)
                     -> MemoryStore (raw turns / memories)
                     -> run_consolidation_pass (due check -> batch extract)
                     -> run_forgetting_pass (decay / raw TTL)
                     -> recall injection into ProviderRequest

Only the background LLM call is faked (deterministic, prompt-driven JSON),
since consolidation quality itself depends on a real model. Everything
else — event resolution, gating, thresholds, watermarks, reinforcement,
decay, bridging — is the production code path.
"""

import json
import time

import pytest
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    Group,
    MessageMember,
)
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest

from core import consolidation as consolidation_module
from core.consolidation import run_consolidation_pass, run_forgetting_pass
from core.event_handler import EventHandler
from core.storage import MemoryStore

PLATFORM = "test"

# Small thresholds so a handful of rounds triggers consolidation like
# the 30-min scheduler would in production.
BASE_CONFIG = {
    "enable_private_memory": True,
    "enable_group_memory": True,
    "background_llm_provider": "",
    "recall_top_k": 5,
    "recall_core_top_k": 2,
    "recall_recent_turns": 3,
    "consolidation_scan_interval_minutes": 30,
    "consolidation_count_threshold_private": 2,
    "consolidation_count_threshold_group": 3,
    "consolidation_idle_hours": 12,
    "raw_retention_days": 14,
    "decay_rate_semantic": 0.98,
    "decay_rate_insight": 0.995,
    "enable_cross_scope_bridge": False,
    "bridge_max_sensitivity": "low",
    "group_capture_ignored_users": [],
    "group_capture_ignored_keywords": [],
}


# ==================== fakes & builders ====================


class _Event(AstrMessageEvent):
    async def send(self, message):
        await super().send(message)


def make_event(
    text: str,
    *,
    private: bool = True,
    sender_id: str = "u1",
    nickname: str = "小张",
    group_id: str = "g1",
    images: int = 0,
) -> AstrMessageEvent:
    """Build a real AstrMessageEvent the same way astrbot core tests do."""
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    msg.self_id = "bot"
    msg.session_id = sender_id if private else group_id
    msg.message_id = f"m{time.time_ns()}"
    msg.sender = MessageMember(user_id=sender_id, nickname=nickname)
    chain = [Plain(text=text)]
    chain.extend(Image(file="x.jpg") for _ in range(images))
    msg.message = chain
    msg.message_str = text
    if not private:
        msg.group = Group(group_id=group_id)
    platform_meta = PlatformMetadata(name=PLATFORM, description="", id=PLATFORM)
    return _Event(
        message_str=text,
        message_obj=msg,
        platform_meta=platform_meta,
        session_id=msg.session_id,
    )


def make_resp(text: str) -> LLMResponse:
    resp = LLMResponse(role="assistant")
    resp.completion_text = text
    return resp


def injected_text(req: ProviderRequest) -> str:
    return "".join(part.text for part in req.extra_user_content_parts)


class FakeConsolidator:
    """Scripted background LLM: records prompts, replays canned JSON outputs."""

    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def __call__(self, context, config, *, prompt, system_prompt, event=None):
        self.prompts.append(prompt)
        self.systems.append(system_prompt)
        assert self.outputs, "fake LLM received more calls than scripted outputs"
        return self.outputs.pop(0)


async def _make_store() -> MemoryStore:
    store = MemoryStore(":memory:")
    await store.initialize()
    return store


async def _backdate_memory(store: MemoryStore, memory_id: int, days_ago: float) -> None:
    ts = int(time.time() - days_ago * 86400)
    await store.connection.execute(
        "UPDATE memories SET updated_at = ? WHERE id = ?", (ts, memory_id)
    )
    await store.connection.commit()


async def _backdate_all_raw_turns(store: MemoryStore, days_ago: float) -> None:
    ts = int(time.time() - days_ago * 86400)
    await store.connection.execute("UPDATE raw_turns SET created_at = ?", (ts,))
    await store.connection.commit()


# ==================== private chat: full loop ====================


@pytest.mark.asyncio
async def test_private_memory_full_loop(monkeypatch):
    store = await _make_store()
    handler = EventHandler(None, dict(BASE_CONFIG), store)
    event = make_event("我养了一只柴犬，叫小福")

    # Round 1: nothing consolidated yet, so recall injects nothing
    req = ProviderRequest(prompt=event.message_str)
    await handler.on_llm_request(event, req)
    assert req.extra_user_content_parts == []
    await handler.on_llm_response(event, make_resp("小福真可爱！"))

    # Round 2: multimedia becomes a placeholder in the captured raw turn
    event2 = make_event("小福最爱吃鸡肉干", images=1)
    await handler.on_llm_response(event2, make_resp("狗狗都爱吃零食"))
    _, total = await store.get_raw_turns("private", "test:u1")
    assert total == 2
    turns, _ = await store.get_raw_turns("private", "test:u1")
    assert any("[图片]" in t["content"] for t in turns)

    # Threshold reached -> consolidation extracts a semantic memory + insight
    fake = FakeConsolidator(
        json.dumps(
            {
                "semantic_ops": [
                    {
                        "action": "insert",
                        "content": "用户养了一只柴犬，名叫小福",
                        "tags": "柴犬,小狗,狗,宠物",
                        "subject": None,
                        "importance": 4,
                    }
                ],
                "insight": {"content": "用户喜欢宠物，愿意聊宠物日常", "importance": 4},
            }
        )
    )
    monkeypatch.setattr(consolidation_module, "call_background_llm", fake)
    assert await run_consolidation_pass(None, dict(BASE_CONFIG), store) == 1
    # The consolidation prompt carried the captured turns verbatim
    assert "我养了一只柴犬，叫小福" in fake.prompts[0]
    assert "小福最爱吃鸡肉干" in fake.prompts[0]
    assert "（暂无）" in fake.prompts[0]
    memories = await store.get_scope_memories("private", "test:u1")
    assert {m["memory_type"] for m in memories} == {"semantic", "insight"}
    activity = (await store.get_scope_activity())[0]
    assert activity["pending"] == 0

    # Decay weakens the un-activated memory (3 days at rate 0.5 -> 0.125)...
    mem_id = next(m["id"] for m in memories if m["memory_type"] == "semantic")
    await _backdate_memory(store, mem_id, 3)
    await run_forgetting_pass(dict(BASE_CONFIG, decay_rate_semantic=0.5), store)
    strength = {
        m["id"]: m["strength"]
        for m in await store.get_scope_memories("private", "test:u1")
    }[mem_id]
    assert strength == pytest.approx(0.125)

    # ...then recall on a related cue injects all three layers and reinforces
    req2 = ProviderRequest(prompt="柴犬掉毛严重吗")
    await handler.on_llm_request(event, req2)
    block = injected_text(req2)
    assert "[长期记忆]" in block
    assert "用户养了一只柴犬，名叫小福" in block
    assert "[洞察] 用户喜欢宠物" in block
    assert "[对话]" in block  # cue layer also hits captured raw turns
    reinforced = {
        m["id"]: m["strength"]
        for m in await store.get_scope_memories("private", "test:u1")
    }[mem_id]
    assert reinforced == pytest.approx(1.0)

    # New state over the horizon: two more rounds due the next pass,
    # and the model updates the stale fact by [#id] from the prompt
    await handler.on_llm_response(make_event("我们搬到上海了"), make_resp("恭喜乔迁！"))
    await handler.on_llm_response(make_event("上海的房子不大"), make_resp("温馨就好"))
    fake.outputs.append(
        json.dumps(
            {
                "semantic_ops": [
                    {
                        "action": "update",
                        "target_id": mem_id,
                        "content": "用户已搬到上海，养柴犬小福",
                        "importance": 4,
                    }
                ],
                "insight": None,
            }
        )
    )
    assert await run_consolidation_pass(None, dict(BASE_CONFIG), store) == 1
    contents = {
        m["content"] for m in await store.get_scope_memories("private", "test:u1")
    }
    assert "用户已搬到上海，养柴犬小福" in contents

    # Raw turns past the retention TTL are pruned by the forgetting pass
    await _backdate_all_raw_turns(store, 15)
    assert await run_forgetting_pass(dict(BASE_CONFIG), store) >= 1
    _, total = await store.get_raw_turns("private", "test:u1")
    assert total == 0
    await store.close()


@pytest.mark.asyncio
async def test_private_recall_skips_turns_already_in_context():
    """Private chat: turns visible in req.contexts must not be recalled again."""
    store = await _make_store()
    handler = EventHandler(None, dict(BASE_CONFIG), store)
    event1 = make_event("北京烤鸭真好吃")
    await handler.on_llm_response(event1, make_resp("嗯呢"))
    event2 = make_event("上海的故宫也很棒")
    await handler.on_llm_response(event2, make_resp("确实"))

    # Latest round is already inside the model context: only the older
    # turn may come back through the cue layer
    req = ProviderRequest(
        prompt="故宫门票多少钱",
        contexts=[
            {"role": "user", "content": "上海的故宫也很棒"},
            {"role": "assistant", "content": "确实"},
        ],
    )
    await handler.on_llm_request(event2, req)
    block = injected_text(req)
    assert "故宫" not in block
    assert "烤鸭" in block

    # Without conversation context both turns are eligible again
    req2 = ProviderRequest(prompt="故宫门票多少钱")
    await handler.on_llm_request(event2, req2)
    block2 = injected_text(req2)
    assert "故宫" in block2
    assert "烤鸭" in block2
    await store.close()


@pytest.mark.asyncio
async def test_private_recall_counts_rounds_by_user_role():
    """可见轮次数按 user 消息数计：非成对上下文（工具消息等）不破坏排除"""
    store = await _make_store()
    handler = EventHandler(None, dict(BASE_CONFIG), store)
    for text in ("第一次提到烤鸭", "第二次提到故宫", "第三次提到长城"):
        await handler.on_llm_response(make_event(text), make_resp("好的"))

    # 3 个 user 轮次（夹杂 tool 消息，总条数 5）：3 轮原文都应被排除；
    # 旧逻辑按 len//2 只排除 2 条，最近一轮「长城」会被错误地重复注入
    req = ProviderRequest(
        prompt="长城好玩吗",
        contexts=[
            {"role": "user", "content": "第一次提到烤鸭"},
            {"role": "assistant", "content": "好的"},
            {"role": "tool", "content": "工具结果"},
            {"role": "user", "content": "第二次提到故宫"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "第三次提到长城"},
        ],
    )
    await handler.on_llm_request(make_event("长城好玩吗"), req)
    assert injected_text(req) == ""
    await store.close()


# ==================== group chat: capture, subject, bridge ====================


@pytest.mark.asyncio
async def test_group_capture_consolidation_and_bridge(monkeypatch):
    config = dict(BASE_CONFIG, enable_cross_scope_bridge=True)
    store = await _make_store()
    handler = EventHandler(None, config, store)

    zhang = make_event(
        "我下个月要去北京出差", private=False, sender_id="u_zhang", nickname="张三"
    )
    await handler.on_group_message(zhang)
    await handler.on_group_message(
        make_event("欢迎欢迎", private=False, sender_id="u_li", nickname="李四")
    )
    # Commands are never captured
    await handler.on_group_message(
        make_event("/help", private=False, sender_id="u_zhang", nickname="张三")
    )
    # Bot side of the exchange is captured by the response hook
    await handler.on_llm_response(zhang, make_resp("好的，祝出差顺利！"))
    # Ignore-listed users are dropped at capture time
    handler.config = dict(config, group_capture_ignored_users=["u_li"])
    await handler.on_group_message(
        make_event("这句话不该被记住", private=False, sender_id="u_li", nickname="李四")
    )
    handler.config = config

    turns, total = await store.get_raw_turns("group", "test:g1")
    assert total == 3
    zhang_turn = next(t for t in turns if "北京出差" in t["content"])
    assert zhang_turn["speaker_name"] == "张三"
    assert any("祝出差顺利" in t["content"] for t in turns)
    assert not any("不该被记住" in t["content"] for t in turns)
    assert not any(t["content"].startswith("/help") for t in turns)

    # Consolidation extracts a subject-attributed fact and a self statement
    fake = FakeConsolidator(
        json.dumps(
            {
                "semantic_ops": [
                    {
                        "action": "insert",
                        "content": "[2026-10-01] 张三要去北京出差",
                        "tags": "出差,北京,工作,旅行",
                        "subject": "张三",
                        "importance": 4,
                    }
                ],
                "insight": None,
                "self_statements": [
                    {
                        "turn_id": zhang_turn["id"],
                        "content": "张三下个月要去北京出差",
                        "sensitivity_level": "low",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(consolidation_module, "call_background_llm", fake)
    assert await run_consolidation_pass(None, config, store) == 1
    mems = await store.get_scope_memories("group", "test:g1")
    assert any(m["subject"] == "张三" and "北京出差" in m["content"] for m in mems)

    # Without user consent the self statement must not cross scopes
    bridged, _ = await store.get_raw_turns("private", "test:u_zhang")
    assert bridged == []

    # After consent, a later pass bridges the statement into private memory.
    # A scope-level override lowers the group threshold so one new turn is due.
    await store.set_bridge_enabled(PLATFORM, "u_zhang", True)
    await store.set_scope_config(
        "group", "test:g1", {"consolidation_count_threshold": 1}
    )
    await handler.on_group_message(
        make_event(
            "对了，我出差会带特产回来",
            private=False,
            sender_id="u_zhang",
            nickname="张三",
        )
    )
    new_turns, _ = await store.get_raw_turns("group", "test:g1")
    specialty_turn = next(t for t in new_turns if "带特产" in t["content"])
    fake.outputs.append(
        json.dumps(
            {
                "semantic_ops": [],
                "insight": None,
                "self_statements": [
                    {
                        "turn_id": specialty_turn["id"],
                        "content": "张三出差会带特产回来",
                        "sensitivity_level": "low",
                    }
                ],
            }
        )
    )
    assert await run_consolidation_pass(None, config, store) == 1
    bridged, _ = await store.get_raw_turns("private", "test:u_zhang")
    assert len(bridged) == 1
    assert "带特产回来" in bridged[0]["content"]

    # Group recall: cued subject memory + recent-turn layer (group only)
    req = ProviderRequest(prompt="出差要准备什么")
    await handler.on_llm_request(
        make_event(
            "出差要准备什么", private=False, sender_id="u_zhang", nickname="张三"
        ),
        req,
    )
    block = injected_text(req)
    assert "[认知] [2026-10-01] 张三要去北京出差（张三）" in block
    assert "[对话]" in block
    await store.close()


# ==================== gating: scope override & global switches ====================


@pytest.mark.asyncio
async def test_group_paths_skip_when_group_id_missing():
    """适配器未提供群号时，捕获与召回都跳过，避免混入同一记忆池"""
    store = await _make_store()
    handler = EventHandler(None, dict(BASE_CONFIG), store)
    event = make_event("群聊消息", private=False)
    event.message_obj.group = None

    await handler.on_group_message(event)
    req = ProviderRequest(prompt="群聊消息")
    await handler.on_llm_request(event, req)
    await handler.on_llm_response(event, make_resp("好的"))

    assert await store.list_scopes() == []
    assert req.extra_user_content_parts == []
    await store.close()


@pytest.mark.asyncio
async def test_scope_override_gates_capture_and_recall():
    store = await _make_store()
    handler = EventHandler(None, dict(BASE_CONFIG), store)
    event = make_event("聊点私事")

    await store.set_scope_config("private", "test:u1", {"enabled": False})
    await handler.on_llm_response(event, make_resp("好的"))
    _, total = await store.get_raw_turns("private", "test:u1")
    assert total == 0
    req = ProviderRequest(prompt="聊点私事")
    await handler.on_llm_request(event, req)
    assert req.extra_user_content_parts == []

    # Clearing the override restores inheritance from global defaults
    await store.set_scope_config("private", "test:u1", {})
    await handler.on_llm_response(event, make_resp("好的"))
    _, total = await store.get_raw_turns("private", "test:u1")
    assert total == 1

    # Global kill switch for private memory
    handler.config = dict(BASE_CONFIG, enable_private_memory=False)
    await handler.on_llm_response(make_event("再来一句"), make_resp("好的"))
    _, total = await store.get_raw_turns("private", "test:u1")
    assert total == 1
    await store.close()
