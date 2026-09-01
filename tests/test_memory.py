"""Memoir core logic tests: term extraction, relevance search, consolidation ops.

Covers the pieces of the memory loop that are pure logic or storage-level:
cue extraction, LIKE-based relevance ranking, recall reinforcement vs
decay interaction, semantic capacity pruning, and LLM op validation.
"""

import pytest

from core.consolidation import _apply_insight, _apply_semantic_ops
from core.memory_recall import extract_terms
from core.storage import MemoryStore


# ==================== extract_terms ====================


def test_extract_terms_cjk_bigrams():
    terms = extract_terms("用户去北京出差")
    assert "北京" in terms
    assert "京出" in terms


def test_extract_terms_filters_stopwords_and_single_chars():
    terms = extract_terms("我在的")
    # single-char CJK stopwords are dropped
    assert "我" not in terms
    assert "在" not in terms


def test_extract_terms_ascii_words():
    terms = extract_terms("I love GPT-4o and LLM")
    # ASCII words keep their original casing; stopword/short words are dropped
    assert "love" in terms
    assert "GPT" in terms
    assert "4o" in terms
    assert "LLM" in terms


def test_extract_terms_strips_urls_and_placeholders():
    terms = extract_terms("看这个 https://example.com/path [图片]")
    assert "https" not in terms
    assert "example" not in terms
    assert "图片" not in terms


def test_extract_terms_caps_length():
    text = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸"
    assert len(extract_terms(text)) <= 12


# ==================== store: search & relevance ====================


async def _make_store() -> MemoryStore:
    store = MemoryStore(":memory:")
    await store.initialize()
    return store


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_insert_raw_turn_touches_scope_in_one_call():
    store = await _make_store()
    await store.insert_raw_turn(
        scope_type="group", scope_key="g:1", content="大家好", speaker_name="张三"
    )
    # scope 活跃记录随插入一并 upsert，巩固扫描能看到待处理轮次
    activity = await store.get_scope_activity()
    assert len(activity) == 1
    assert activity[0]["scope_key"] == "g:1"
    assert activity[0]["pending"] == 1
    await store.close()


@pytest.mark.asyncio
async def test_scope_config_cache_invalidation():
    store = await _make_store()
    assert await store.get_scope_config("private", "p:1") == {}
    await store.set_scope_config("private", "p:1", {"enabled": False})
    assert await store.get_scope_config("private", "p:1") == {"enabled": False}
    assert ("private", "p:1") in await store.get_all_scope_configs()
    # 空覆盖 = 删除并回到继承全局
    await store.set_scope_config("private", "p:1", {})
    assert await store.get_scope_config("private", "p:1") == {}
    await store.close()


@pytest.mark.asyncio
async def test_search_prefers_multi_term_and_tag_hits():
    store = await _make_store()
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="用户养了一只柴犬，名叫小福",
        tags="柴犬,小狗,狗,宠物",
    )
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="用户想养一只小狗",
        tags="小狗,宠物,计划",
    )
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="用户在杭州工作",
        tags="工作,杭州",
    )
    hits = await store.search_memories("private", "p:1", ["柴犬", "小狗"], top_k=5)
    # both terms hit the first memory (content + tags), only one hits the second
    assert hits[0]["content"] == "用户养了一只柴犬，名叫小福"
    assert len(hits) == 2
    await store.close()


@pytest.mark.asyncio
async def test_search_scoped_isolation():
    store = await _make_store()
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="用户在北京工作",
        tags="北京",
    )
    assert await store.search_memories("private", "p:2", ["北京"], top_k=5) == []
    await store.close()


@pytest.mark.asyncio
async def test_search_raw_hits_raw_turns():
    store = await _make_store()
    await store.insert_raw_turn(
        scope_type="group",
        scope_key="g:1",
        content="明天下午三点开会",
        speaker_name="张三",
    )
    hits = await store.search_raw("group", "g:1", ["开会"], top_k=3)
    assert len(hits) == 1
    assert "开会" in hits[0]["content"]
    await store.close()


# ==================== store: reinforce vs decay ====================


@pytest.mark.asyncio
async def test_reinforce_resets_decay_base():
    store = await _make_store()
    mid = await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="老认知",
    )
    await store.decay_and_forget(0.5, 0.5)
    await store.reinforce_memories([mid])
    await store.decay_and_forget(0.5, 0.5)
    rows = await store.get_scope_memories("private", "p:1")
    # after reinforcement one decay pass halves 1.0 -> 0.5;
    # the old elapsed-based formula would stack extra decay on the stale base
    assert rows[0]["strength"] == pytest.approx(0.5)
    await store.close()


# ==================== store: semantic capacity ====================


@pytest.mark.asyncio
async def test_prune_semantic_keeps_top_ranked():
    store = await _make_store()
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="重要",
        importance=5,
    )
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="次要",
        importance=1,
    )
    await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="中等",
        importance=3,
    )
    deleted = await store.prune_semantic("private", "p:1", cap=2)
    assert deleted == 1
    rows = await store.get_scope_memories("private", "p:1")
    assert {r["content"] for r in rows} == {"重要", "中等"}
    await store.close()


# ==================== consolidation ops ====================


@pytest.mark.asyncio
async def test_apply_semantic_ops_insert_update_expire():
    store = await _make_store()
    target = await store.insert_memory(
        scope_type="private",
        scope_key="p:1",
        memory_type="semantic",
        content="用户在北京工作",
    )
    parsed = {
        "semantic_ops": [
            {
                "action": "update",
                "target_id": target,
                "content": "用户已搬到上海",
                "importance": 4,
            },
            {
                "action": "insert",
                "content": "[2026-09-07] 用户去北京出差",
                "tags": "出差,北京,工作,旅行",
                "subject": None,
                "importance": 3,
            },
            {"action": "expire", "target_id": target},
            {"action": "bogus"},
        ]
    }
    semantic = [{"id": target, "memory_type": "semantic"}]
    await _apply_semantic_ops(store, "private", "p:1", parsed, semantic)
    rows = await store.get_scope_memories("private", "p:1")
    # updated memory was then expired; only the insert survives
    assert len(rows) == 1
    assert "去北京出差" in rows[0]["content"]
    assert rows[0]["tags"] == "出差,北京,工作,旅行"
    await store.close()


@pytest.mark.asyncio
async def test_apply_semantic_ops_rejects_cross_scope_ids():
    store = await _make_store()
    other_scope_id = await store.insert_memory(
        scope_type="private",
        scope_key="p:2",
        memory_type="semantic",
        content="另一个会话的认知",
    )
    parsed = {
        "semantic_ops": [
            {"action": "update", "target_id": other_scope_id, "content": "越权覆盖"},
            {"action": "expire", "target_id": other_scope_id},
        ]
    }
    await _apply_semantic_ops(store, "private", "p:1", parsed, [])
    rows = await store.get_scope_memories("private", "p:2")
    assert len(rows) == 1
    assert rows[0]["content"] == "另一个会话的认知"
    await store.close()


@pytest.mark.asyncio
async def test_apply_insight_inserts_insight():
    store = await _make_store()
    await _apply_insight(
        store,
        "private",
        "p:1",
        {"insight": {"content": "用户工作变动频繁", "importance": 4}},
    )
    rows = await store.get_scope_memories("private", "p:1")
    assert rows[0]["memory_type"] == "insight"
    assert rows[0]["importance"] == 4
    await store.close()
