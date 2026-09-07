"""Regression coverage for storage, lifecycle, provider and queue correctness."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot.api.provider import ProviderRequest
from test_pipeline_e2e import BASE_CONFIG, injected_text, make_event, make_resp

from core import consolidation, media_processor
from core.consolidation import _bridge_self_statements, _consolidate_scope
from core.event_handler import EventHandler
from core.llm_helper import call_background_llm
from core.storage import SCHEMA_SQL, MemoryStore


@pytest.mark.asyncio
async def test_web_api_scope_boundaries_and_retry_switches(store):
    from astrbot.api.web import PluginRequest, bind_request_context
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from core.web_api import WebApi

    class Plugin:
        pass

    plugin = Plugin()
    plugin.store, plugin.config = store, {}
    plugin._initialized, plugin._terminating = True, False
    plugin.event_handler = EventHandler(None, plugin.config, store)
    api = WebApi(plugin)
    app = FastAPI()

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await getattr(api, action)()

    raw_id = await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="Private original text"
    )
    memory_id = await store.insert_memory(
        scope_type="private",
        scope_key="test:u1",
        memory_type="semantic",
        content="A fact",
        source_ref=json.dumps([raw_id]),
    )
    work_id = await store.record_work(
        "consolidation", "private", "test:u1", [raw_id], error="Invalid model output"
    )
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        wrong_scope = {"scope_type": "private", "scope_key": "test:u2"}
        scope = {"scope_type": "private", "scope_key": "test:u1"}
        response = await client.get(
            "/memory_sources", params={**wrong_scope, "id": memory_id}
        )
        assert response.status_code == 400
        response = await client.get(
            "/memory_sources", params={**scope, "id": memory_id}
        )
        assert response.status_code == 200
        assert "Private original text" in response.text
        response = await client.get("/processing_status", params=wrong_scope)
        assert response.json()["failures"] == []
        response = await client.post(
            "/retry_processing", json={**wrong_scope, "id": work_id}
        )
        assert response.status_code == 400
        await store.set_scope_config("private", "test:u1", {"enabled": False})
        response = await client.post("/retry_processing", json={**scope, "id": work_id})
        assert response.status_code == 400
        await store.set_scope_config("private", "test:u1", {})
        plugin.config["enable_private_memory"] = False
        response = await client.post("/retry_processing", json={**scope, "id": work_id})
        assert response.status_code == 400
        response = await client.post(
            "/update_global_config", json={"enable_private_memory": True}
        )
        assert response.status_code == 200
        assert store.config_revision == 1
        response = await client.post("/retry_processing", json={**scope, "id": work_id})
        assert response.status_code == 200
        assert plugin.event_handler.scheduler.wakeup.is_set()


@pytest.mark.asyncio
async def test_invalid_attachment_becomes_visible_failure(store):
    raw_id = await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="[Image]"
    )
    await store.record_work(
        "media",
        "private",
        "test:u1",
        [raw_id],
        payload={
            "revision": 0,
            "parts": [{"kind": "image", "file": {"invalid": True}}],
        },
    )
    processor = media_processor.MediaProcessor(None, {}, store)
    assert await processor.process_once()
    status = await store.get_processing_status("private", "test:u1")
    assert len(status["failures"]) == 1
    assert "Media processing failed" in status["failures"][0]["error"]
    assert not await processor.process_once()


@pytest.mark.asyncio
async def test_search_without_speaker_does_not_boost_shared_facts(store):
    for owner in ("u1", ""):
        await store.insert_memory(
            scope_type="group",
            scope_key="test:g",
            memory_type="semantic",
            subject_id=owner,
            content="Photography",
        )
    results = await store.search_memories("group", "test:g", ["Photography"])
    assert results[0]["relevance"] == results[1]["relevance"]


@pytest_asyncio.fixture
async def store():
    instance = MemoryStore(":memory:")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_zero_retention_only_applies_capacity(store):
    for i in range(3):
        await store.insert_raw_turn(
            scope_type="private", scope_key="test:u1", content=str(i)
        )
    await store.connection.execute(
        "UPDATE raw_turns SET created_at=?", (int(time.time()) - 86400,)
    )
    assert await store.prune_raw(0, cap_per_scope=2) == 1
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 2
    assert await store.prune_raw(3600) == 2


@pytest.mark.asyncio
async def test_keys_are_isolated_by_stable_person_and_group_facts(store):
    for owner, name, content in [
        ("u1", "Same name", "Teacher"),
        ("u2", "Same name", "Doctor"),
        ("", None, "Group topic"),
    ]:
        await store.insert_memory(
            scope_type="group",
            scope_key="test:g",
            memory_type="semantic",
            subject_id=owner,
            subject=name,
            memory_key="user:job",
            content=content,
        )
    await store.insert_memory(
        scope_type="group",
        scope_key="test:g",
        memory_type="semantic",
        subject_id="u1",
        subject="Renamed",
        memory_key="user:job",
        content="Engineer",
    )
    rows = await store.get_scope_memories("group", "test:g")
    assert {r["subject_id"]: r["content"] for r in rows} == {
        "u1": "Engineer",
        "u2": "Doctor",
        "": "Group topic",
    }
    assert next(r for r in rows if r["subject_id"] == "u1")["subject"] == "Renamed"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["clear", "disable", "delete_raw", "global_change"])
async def test_inflight_consolidation_cannot_restore_invalidated_data(
    store, monkeypatch, action
):
    raw_id = await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="I am a teacher"
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return json.dumps(
            {
                "semantic_ops": [
                    {"action": "insert", "key": "user:job", "content": "Teacher"}
                ]
            }
        )

    monkeypatch.setattr(consolidation, "call_background_llm", delayed)
    task = asyncio.create_task(
        _consolidate_scope(None, {}, store, "private", "test:u1")
    )
    await started.wait()
    if action == "clear":
        await store.delete_scope_memories("private", "test:u1")
    elif action == "disable":
        await store.set_scope_config("private", "test:u1", {"enabled": False})
    elif action == "delete_raw":
        await store.delete_raw_turn_in_scope(raw_id, "private", "test:u1")
    else:
        store.config_revision += 1
    release.set()
    await task
    assert await store.get_scope_memories("private", "test:u1") == []


@pytest.mark.asyncio
async def test_transaction_rolls_back_the_whole_batch_and_releases_lock(store):
    with pytest.raises(RuntimeError):
        async with store.transaction():
            await store.insert_memory(
                scope_type="private",
                scope_key="test:u1",
                memory_type="semantic",
                content="Must roll back",
            )
            raise RuntimeError("simulated failure after insert")
    assert await store.get_scope_memories("private", "test:u1") == []
    await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="Still usable"
    )


@pytest.mark.asyncio
async def test_cancelled_transaction_cannot_commit_partial_results(store):
    entered = asyncio.Event()

    async def work():
        async with store.transaction():
            await store.insert_raw_turn(
                scope_type="private", scope_key="test:u1", content="Cancelled"
            )
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "{}",
        '{"semantic_ops": null}',
        '{"semantic_ops":[{"action":"insert","content":"No key"}]}',
        '{"semantic_ops":[{"action":"expire","target_id":999}]}',
        '{"semantic_ops":[{"action":"insert","key":"job","content":"Teacher","subject_id":["u1"]}]}',
        '{"semantic_ops":[{"action":"insert","key":"job","content":"Teacher","subject":{"id":"u1"}}]}',
    ],
)
async def test_bad_shapes_quarantine_without_claiming_success_and_can_retry(
    store, monkeypatch, output
):
    await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="Teacher"
    )
    monkeypatch.setattr(
        consolidation, "call_background_llm", AsyncMock(return_value=output)
    )
    await _consolidate_scope(None, {}, store, "private", "test:u1")
    rows, _ = await store.get_raw_turns("private", "test:u1")
    assert rows[0]["extracted"] == -1
    failure = (await store.get_processing_status("private", "test:u1"))["failures"][0]
    assert not await store.retry_work(failure["id"], "private", "test:other")
    assert await store.retry_work(failure["id"], "private", "test:u1")
    monkeypatch.setattr(
        consolidation,
        "call_background_llm",
        AsyncMock(return_value='{"semantic_ops":[],"insight":null}'),
    )
    await _consolidate_scope(None, {}, store, "private", "test:u1")
    assert (await store.get_raw_turns("private", "test:u1"))[0][0]["extracted"] == 1


@pytest.mark.asyncio
async def test_global_and_bridge_target_switches_are_enforced(store, monkeypatch):
    await store.insert_raw_turn(
        scope_type="group", scope_key="test:g", content="Teacher"
    )
    llm = AsyncMock()
    monkeypatch.setattr(consolidation, "call_background_llm", llm)
    await consolidation.run_consolidation_pass(
        None,
        {"enable_group_memory": False, "consolidation_count_threshold_group": 1},
        store,
    )
    llm.assert_not_awaited()
    await store.set_bridge_enabled("test", "u1", True)
    parsed = {
        "self_statements": [
            {"turn_id": 1, "content": "Teacher", "sensitivity_level": "low"}
        ]
    }
    turn_map = {1: {"speaker_id": "u1"}}
    await _bridge_self_statements(
        {"enable_cross_scope_bridge": True, "enable_private_memory": False},
        store,
        "test:g",
        parsed,
        turn_map,
    )
    await store.set_scope_config("private", "test:u1", {"enabled": False})
    await _bridge_self_statements(
        {"enable_cross_scope_bridge": True}, store, "test:g", parsed, turn_map
    )
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 0


@pytest.mark.asyncio
async def test_session_is_persisted_and_used_by_scheduled_consolidation(
    store, monkeypatch
):
    event = make_event("Teacher")
    handler = EventHandler(None, BASE_CONFIG, store)
    await handler.on_llm_response(event, make_resp("Noted"))
    llm = AsyncMock(return_value='{"semantic_ops":[]}')
    monkeypatch.setattr(consolidation, "call_background_llm", llm)
    await _consolidate_scope(None, {}, store, "private", "test:u1")
    assert llm.call_args.kwargs["event"].unified_msg_origin == event.unified_msg_origin
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=make_resp("ok")))
    context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
    assert (
        await call_background_llm(
            context, {"_require_session": True}, prompt="test", event=event
        )
        == "ok"
    )
    context.get_using_provider_async.assert_awaited_once_with(
        umo=event.unified_msg_origin
    )
    context.get_using_provider_async.reset_mock()
    assert (
        await call_background_llm(context, {"_require_session": True}, prompt="test")
        is None
    )
    context.get_using_provider_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_capture_does_not_wait_for_media_and_clear_cancels_result(
    store, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()

    async def describe(*args, **kwargs):
        started.set()
        await release.wait()
        return "A dog"

    monkeypatch.setattr(media_processor, "describe_multimedia", describe)
    handler = EventHandler(None, BASE_CONFIG, store)
    await asyncio.wait_for(
        handler.on_llm_response(make_event("Photo", images=1), make_resp("Nice")),
        timeout=1,
    )
    assert not started.is_set()
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 1
    assert await store.get_pending_raw("private", "test:u1") == []
    task = asyncio.create_task(handler.media.process_once())
    await started.wait()
    await store.delete_scope_memories("private", "test:u1")
    release.set()
    await task
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 0


@pytest.mark.asyncio
async def test_media_queue_is_bounded_and_failed_media_can_retry(store, monkeypatch):
    handler = EventHandler(None, BASE_CONFIG, store)
    for _ in range(33):
        await handler.on_group_message(make_event("Photo", private=False, images=1))
    status = await store.get_processing_status("group", "test:g1")
    assert next(c["count"] for c in status["counts"] if c["status"] == "pending") == 32
    failed = status["failures"][0]
    assert not await store.retry_work(failed["id"], "group", "test:g1")
    monkeypatch.setattr(
        media_processor, "describe_multimedia", AsyncMock(return_value="A dog")
    )
    assert await handler.media.process_once()
    assert await store.retry_work(failed["id"], "group", "test:g1")


@pytest.mark.asyncio
async def test_sources_and_recall_budget(store, monkeypatch):
    raw_id = await store.insert_raw_turn(
        scope_type="private", scope_key="test:u1", content="Teacher"
    )
    monkeypatch.setattr(
        consolidation,
        "call_background_llm",
        AsyncMock(
            return_value=json.dumps(
                {
                    "semantic_ops": [
                        {
                            "action": "insert",
                            "key": "user:job",
                            "content": "Teacher",
                            "source_turn_ids": [raw_id],
                        }
                    ]
                }
            )
        ),
    )
    await _consolidate_scope(None, {}, store, "private", "test:u1")
    memory_id = (await store.get_scope_memories("private", "test:u1"))[0]["id"]
    assert (await store.get_memory_sources(memory_id, "private", "test:u1"))["items"][
        0
    ]["id"] == raw_id
    assert await store.get_memory_sources(memory_id, "private", "test:other") is None
    await store.delete_raw_turn_in_scope(raw_id, "private", "test:u1")
    assert (await store.get_memory_sources(memory_id, "private", "test:u1"))[
        "expired"
    ] == 1
    for _ in range(8):
        await store.insert_raw_turn(
            scope_type="group", scope_key="test:g1", content="Teacher " * 250
        )
    handler = EventHandler(None, dict(BASE_CONFIG, recall_max_chars=512), store)
    request = ProviderRequest(prompt="Teacher")
    await handler.on_llm_request(make_event("Teacher", private=False), request)
    assert 0 < len(injected_text(request)) <= 512


@pytest.mark.asyncio
async def test_old_response_and_queued_capture_cannot_restore_cleared_scope(store):
    handler = EventHandler(None, BASE_CONFIG, store)
    event = make_event("Teacher")
    await handler.on_llm_request(event, ProviderRequest(prompt="Teacher"))
    await store.delete_scope_memories("private", "test:u1")
    await handler.on_llm_response(event, make_resp("Old response"))
    assert (await store.get_raw_turns("private", "test:u1"))[1] == 0
    await store.delete_scope_memories("group", "test:g1")
    await handler.on_group_message(
        make_event("Old message", private=False), generation=0
    )
    assert (await store.get_raw_turns("group", "test:g1"))[1] == 0


@pytest.mark.asyncio
async def test_legacy_database_migration_is_lossless_and_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            SCHEMA_SQL.replace("    subject_id TEXT NOT NULL DEFAULT '',\n", "")
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_memories_scope_key ON memories(scope_type,scope_key,memory_key) WHERE memory_key IS NOT NULL"
        )
        connection.execute(
            "INSERT INTO memories(id,scope_type,scope_key,memory_type,memory_key,subject,content,created_at,updated_at) VALUES(7,'group','test:g','semantic','user:job','Alice','Teacher',1,1)"
        )
        connection.execute(
            "INSERT INTO raw_turns(id,scope_type,scope_key,content,created_at) VALUES(9,'group','test:g','Original',1)"
        )
        connection.execute(
            "INSERT INTO consolidation_failures(scope_type,scope_key,turn_ids,llm_output,created_at) VALUES('group','test:g','[9]','invalid',1)"
        )
    for _ in range(2):
        migrated = MemoryStore(db)
        await migrated.initialize()
        try:
            rows = await migrated.get_scope_memories("group", "test:g")
            assert rows[0]["id"] == 7
            assert rows[0]["content"] == "Teacher"
            assert rows[0]["subject_id"] == "legacy:Alice"
            failures = (await migrated.get_processing_status("group", "test:g"))[
                "failures"
            ]
            assert len(failures) == 1
            assert failures[0]["retryable"]
        finally:
            await migrated.close()


@pytest.mark.asyncio
async def test_media_job_survives_restart_and_clears_temporary_references(
    tmp_path, monkeypatch
):
    db = tmp_path / "queue.db"
    initial = MemoryStore(db)
    await initial.initialize()
    handler = EventHandler(None, BASE_CONFIG, initial)
    await handler.on_llm_response(make_event("Photo", images=1), make_resp("Nice"))
    await initial.connection.execute("UPDATE work_items SET status='running'")
    await initial.connection.commit()
    await initial.close()
    reopened = MemoryStore(db)
    await reopened.initialize()
    try:
        monkeypatch.setattr(
            media_processor, "describe_multimedia", AsyncMock(return_value="A dog")
        )
        processor = media_processor.MediaProcessor(None, BASE_CONFIG, reopened)
        assert await processor.process_once()
        assert (
            "A dog"
            in (await reopened.get_raw_turns("private", "test:u1"))[0][0]["content"]
        )
        cursor = await reopened.connection.execute(
            "SELECT status,payload FROM work_items"
        )
        job = await cursor.fetchone()
        assert tuple(job) == ("complete", "{}")
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_two_workers_enforce_actual_concurrency(store, monkeypatch):
    active, peak = 0, 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def describe(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
            return "A dog"
        finally:
            active -= 1

    monkeypatch.setattr(media_processor, "describe_multimedia", describe)
    handler = EventHandler(None, BASE_CONFIG, store)
    for _ in range(4):
        await handler.on_group_message(make_event("Photo", private=False, images=1))
    handler.media.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        cursor = await store.connection.execute(
            "SELECT status,COUNT(*) FROM work_items GROUP BY status"
        )
        assert dict(await cursor.fetchall()) == {"running": 2, "pending": 2}
        assert peak == 2
    finally:
        release.set()
        await handler.media.stop()


@pytest.mark.asyncio
async def test_large_attachment_is_rejected_before_model_call(tmp_path, monkeypatch):
    from astrbot.api.message_components import Image

    from core.llm_helper import describe_multimedia

    file = tmp_path / "large.png"
    with file.open("wb") as stream:
        stream.truncate(11 * 1024 * 1024)
    event = make_event("Photo")
    event.message_obj.message = [Image.fromFileSystem(file)]
    provider = SimpleNamespace(
        provider_config={"modalities": ["image"]}, text_chat=AsyncMock()
    )
    context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
    problems = []
    assert await describe_multimedia(context, {}, event, problems=problems) == ""
    assert "size budget" in problems[0]
    provider.text_chat.assert_not_awaited()
