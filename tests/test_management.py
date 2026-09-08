"""Verify scoped management queries, manual edits and atomic selections."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot.api.message_components import Node, Plain
from astrbot.api.web import PluginRequest, bind_request_context
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from test_forwarding import forward_event
from test_pipeline_e2e import BASE_CONFIG

from core.event_handler import EventHandler
from core.storage import MemoryStore
from core.web_api import WebApi


@pytest_asyncio.fixture
async def store(tmp_path):
    db = MemoryStore(tmp_path / "management.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_filtered_search_is_paginated_and_counts_all_matches(store):
    scope = ("private", "test:u1")
    for i in range(57):
        await store.insert_memory(
            scope_type=scope[0],
            scope_key=scope[1],
            memory_type="semantic",
            content=f"共享标签 {i}",
            importance=4,
        )
    await store.insert_memory(
        scope_type="group",
        scope_key="test:u1",
        memory_type="semantic",
        content="共享标签 外部",
    )
    first = await store.browse_records(
        *scope, "memories", {"q": "共享标签", "importance": 4}, 1
    )
    third = await store.browse_records(
        *scope, "memories", {"q": "共享标签", "importance": 4}, 3
    )
    assert first["total"] == third["total"] == 57
    assert len(first["items"]) == 20 and len(third["items"]) == 17
    assert not {row["id"] for row in first["items"]} & {
        row["id"] for row in third["items"]
    }
    assert (await store.browse_records(*scope, "memories", {"q": "%"}))["total"] == 0
    assert (await store.browse_records(*scope, "memories", {"memory_type": "insight"}))[
        "total"
    ] == 0


@pytest.mark.asyncio
async def test_grouped_events_find_tail_chunks_and_return_complete_source(store):
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    event = forward_event(
        [Node(name="引用作者", content=[Plain(text="长文" * 3000 + "尾部特征")])]
    )
    await handler.on_group_message(event)
    await handler.media.process_once()
    result = await store.browse_records(
        "group",
        "test:g1",
        "raw",
        {"q": "尾部特征", "source": "forwarded", "sender": "u1", "status": "complete"},
        page_size=1,
    )
    assert result["total"] == 1
    root = result["items"][0]
    assert root["parent_id"] is None and root["chunk_count"] > 1
    detail = await store.get_raw_event(root["id"], "group", "test:g1")
    assert len(detail["items"]) == root["chunk_count"]
    assert "尾部特征" in detail["items"][-1]["content"]
    child = detail["items"][-1]["id"]
    assert (await store.get_raw_event(child, "group", "test:g1"))["root"]["id"] == root[
        "id"
    ]
    assert await store.get_raw_event(child, "private", "test:g1") is None
    stats = (await store.list_scopes())[0]
    assert stats["raw_count"] == 1 and stats["chunk_count"] == len(detail["items"])
    assert (
        await store.browse_records(
            "group", "test:g1", "raw", {"since": root["created_at"] + 1}
        )
    )["total"] == 0


@pytest.mark.asyncio
async def test_edits_retain_reference_identity_and_reject_stale_revisions(store):
    memory = await store.insert_memory(
        scope_type="private",
        scope_key="test:u1",
        memory_type="semantic",
        content="引用资料",
        source_type="forwarded",
        source_ref="[42]",
        memory_key="quote",
    )
    before = await store.get_revision("private", "test:u1")
    assert await store.edit_memory(
        memory, "private", "test:u1", "人工修正", "标签", 5, 0
    )
    assert not await store.edit_memory(
        memory, "private", "test:u1", "过时修改", "", 1, 0
    )
    row = (await store.browse_records("private", "test:u1", "memories", {}))["items"][0]
    assert row["content"] == "人工修正" and row["source_type"] == "forwarded"
    assert row["source_ref"] == "[42]" and row["memory_key"] == "forward:quote"
    assert row["manually_edited_at"] and row["edit_revision"] == 1
    assert await store.get_revision("private", "test:u1") == before + 1
    await store.update_memory_content(memory, "后台更新")
    assert not await store.edit_memory(
        memory, "private", "test:u1", "覆盖后台", "", 3, 1
    )


@pytest.mark.asyncio
async def test_atomic_bulk_delete_rejects_foreign_ids_and_clamps_last_page(store):
    ids = [
        await store.insert_raw_turn(
            scope_type="private", scope_key="test:u1", content=str(i)
        )
        for i in range(21)
    ]
    other = await store.insert_raw_turn(
        scope_type="group", scope_key="test:u1", content="其他会话"
    )
    assert not await store.delete_records("raw", [ids[0], other], "private", "test:u1")
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 21
    assert await store.delete_records("raw", ids[:2], "private", "test:u1")
    result = await store.browse_records("private", "test:u1", "raw", {}, 2)
    assert result["page"] == 1 and result["total"] == 19


@pytest.mark.asyncio
async def test_management_http_validation_scope_and_effective_settings(store):
    class Plugin:
        pass

    plugin = Plugin()
    plugin.store, plugin.config = (
        store,
        {"enable_private_memory": False, "consolidation_count_threshold_private": 25},
    )
    plugin._initialized, plugin._terminating = True, False
    api = WebApi(plugin)
    app = FastAPI()

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await getattr(api, action)()

    scope = {"scope_type": "private", "scope_key": "test:u1"}
    memory = await store.insert_memory(
        **scope, memory_type="semantic", content="旧内容"
    )
    await store.set_scope_config(*scope.values(), {"enabled": True})
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        effective = (await client.get("/get_scope_config", params=scope)).json()[
            "effective"
        ]
        assert (
            effective["enabled"] is False
            and effective["consolidation_count_threshold"] == 25
        )
        response = await client.post(
            "/edit_memory",
            json={**scope, "id": memory, "revision": 0, "content": "", "importance": 3},
        )
        assert response.status_code == 400
        response = await client.post(
            "/edit_memory",
            json={
                **scope,
                "scope_type": "group",
                "id": memory,
                "revision": 0,
                "content": "非法跨会话",
                "importance": 3,
            },
        )
        assert response.status_code == 400
        response = await client.post(
            "/edit_memory",
            json={
                **scope,
                "id": memory,
                "revision": 0,
                "content": "新内容",
                "importance": 3,
                "source_type": "forwarded",
            },
        )
        assert response.status_code == 200
        result = (
            await client.get(
                "/browse", params={**scope, "kind": "memories", "q": "新内容"}
            )
        ).json()
        assert result["total"] == 1 and result["items"][0]["source_type"] == "native"
        for ids in ([True], list(range(101)), "1"):
            response = await client.post(
                "/delete_records", json={**scope, "kind": "raw", "ids": ids}
            )
            assert response.status_code == 400
        for before in (
            "invalid",
            "1:2:3",
            "1:-2",
            "1:0",
            "1:999999999999999999999",
            "１:2",
        ):
            response = await client.get(
                "/browse",
                params={**scope, "kind": "raw", "mode": "cursor", "before": before},
            )
            assert response.status_code == 400
        response = await client.get(
            "/browse", params={**scope, "kind": "memories", "mode": "cursor"}
        )
        assert response.status_code == 400
        response = await client.get(
            "/browse", params={**scope, "kind": "raw", "mode": "cursor"}
        )
        assert response.json() == {"items": [], "has_more": False, "next_cursor": None}


@pytest.mark.asyncio
async def test_history_cursor_ties_deletion_new_arrivals_and_index(store):
    ids = [
        await store.insert_raw_turn(
            scope_type="private", scope_key="test:u1", content=f"消息 {i}"
        )
        for i in range(125)
    ]
    await store.connection.execute("UPDATE raw_turns SET created_at=100")
    await store.connection.commit()
    await store.insert_raw_turn(
        scope_type="group", scope_key="test:u1", content="隔离消息"
    )
    statements = []
    await store.connection.set_trace_callback(statements.append)
    first = await store.browse_records(
        "private", "test:u1", "raw", {}, page_size=40, cursor_mode=True
    )
    assert [row["id"] for row in first["items"]] == ids[-40:][::-1]
    boundary = first["items"][-1]["id"]
    await store.delete_records("raw", [boundary], "private", "test:u1")
    await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="刚收到的消息"
    )
    seen = [row["id"] for row in first["items"]]
    result = first
    while result["has_more"]:
        result = await store.browse_records(
            "private",
            "test:u1",
            "raw",
            {},
            page_size=40,
            cursor_mode=True,
            before=tuple(map(int, result["next_cursor"].split(":"))),
        )
        seen.extend(row["id"] for row in result["items"])
    assert seen == ids[::-1]
    assert len(seen) == len(set(seen)) and result["next_cursor"] is None
    assert not any(
        "OFFSET" in sql or "SELECT COUNT(*) FROM (" in sql for sql in statements
    )
    plan = await store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM raw_turns INDEXED BY idx_raw_history WHERE scope_type=? AND scope_key=? AND parent_id IS NULL AND (created_at,id)<(?,?) ORDER BY created_at DESC,id DESC LIMIT 41",
        ("private", "test:u1", 100, boundary),
    )
    assert any(
        "idx_raw_history" in row[3] and "SEARCH" in row[3]
        for row in await plan.fetchall()
    )


@pytest.mark.asyncio
async def test_cursor_filters_search_children_without_returning_chunks(store):
    handler = EventHandler(SimpleNamespace(), BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event(
            [Node(name="引用作者", content=[Plain(text="长文" * 3000 + "尾部特征")])]
        )
    )
    await handler.media.process_once()
    result = await store.browse_records(
        "group",
        "test:g1",
        "raw",
        {"q": "尾部特征", "source": "forwarded", "sender": "u1", "status": "complete"},
        cursor_mode=True,
    )
    assert len(result["items"]) == 1
    root = result["items"][0]
    assert root["parent_id"] is None and root["chunk_count"] > 1
    assert not result["has_more"] and "total" not in result
    assert not (
        await store.browse_records(
            "group",
            "test:g1",
            "raw",
            {"since": root["created_at"] + 1},
            cursor_mode=True,
        )
    )["items"]


@pytest.mark.asyncio
async def test_queue_status_does_not_expose_attachment_payloads(store):
    raw = await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="图片"
    )
    await store.record_work(
        "media",
        "private",
        "test:u1",
        [raw],
        payload={"file": "secret-path", "revision": 0},
    )
    result = await store.get_processing_status("private", "test:u1")
    assert result["items"][0]["status"] == "pending"
    assert "secret-path" not in json.dumps(result)
