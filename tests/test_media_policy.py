"""Exercise plugin-only cost controls at request and durable worker boundaries."""

import asyncio
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from astrbot.api.message_components import Image, Record
from PIL import Image as PillowImage
from test_pipeline_e2e import BASE_CONFIG, make_event, make_resp

from core.event_handler import EventHandler
from core.llm_helper import describe_multimedia
from core.media_inputs import prepare_input
from core.media_policy import MEDIA_DEFAULTS
from core.scope import merge_scope_config
from core.storage import MemoryStore
from core.web_api import GLOBAL_CONFIG_KEYS, GLOBAL_SELECTS, _validate_config_payload


@pytest_asyncio.fixture
async def store(tmp_path):
    db = MemoryStore(tmp_path / "media.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def setup_media(tmp_path):
    image = tmp_path / "photo.png"
    PillowImage.new("RGB", (1600, 1200), "red").save(image)
    audio = tmp_path / "voice.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000 * 2)
    response = SimpleNamespace(
        role="assistant",
        completion_text="红色图片",
        usage=SimpleNamespace(input_other=20, input_cached=5, output=5),
    )
    provider = SimpleNamespace(
        provider_config={
            "id": "cheap",
            "model": "test",
            "modalities": ["image", "audio"],
        },
        text_chat=AsyncMock(return_value=response),
    )
    context = SimpleNamespace(
        get_using_provider_async=AsyncMock(return_value=provider),
        get_provider_by_id=Mock(return_value=provider),
    )
    event = make_event("photo")
    event.message_obj.message = [Image.fromFileSystem(image)]
    return context, provider, event, image, audio


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config,manual,forward,triggered",
    [
        ({"image_mode": "off"}, True, False, True),
        ({"image_mode": "manual"}, False, False, True),
        ({"image_forward_mode": "manual"}, False, True, True),
        ({"image_forward_mode": "off"}, True, True, True),
        ({"image_group_trigger": "reply"}, False, False, False),
        ({"media_require_model": True}, False, False, True),
        ({"image_max_count": 0}, False, False, True),
    ],
)
async def test_policy_blocks_before_download(
    setup_media, monkeypatch, store, config, manual, forward, triggered
):
    context, provider, event, _, _ = setup_media
    event.memoir_manual, event.memoir_forward, event.memoir_triggered = (
        manual,
        forward,
        triggered,
    )
    download = AsyncMock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(Image, "convert_to_file_path", download)
    issues = []
    assert not await describe_multimedia(
        context, config, event, scope=("group", "test:g"), store=store, problems=issues
    )
    assert issues
    download.assert_not_called()
    provider.text_chat.assert_not_called()


@pytest.mark.asyncio
async def test_count_prefix_and_plugin_only_length_target(setup_media, store):
    context, provider, event, image, _ = setup_media
    event.message_obj.message = [Image.fromFileSystem(image) for _ in range(6)]
    issues = []
    assert await describe_multimedia(
        context,
        {"image_max_count": 2, "media_output_tokens": 100},
        event,
        store=store,
        problems=issues,
    )
    request = provider.text_chat.call_args.kwargs
    assert len(request["image_urls"]) == 2
    assert "100 token" in request["system_prompt"]
    assert "max_tokens" not in request
    assert "custom_extra_body" not in provider.provider_config
    assert len(issues) == 4


@pytest.mark.asyncio
async def test_resizing_duration_and_temporary_cleanup(setup_media, store):
    context, provider, event, image, audio = setup_media

    async def inspect(**request):
        with PillowImage.open(request["image_urls"][0]) as scaled:
            assert max(scaled.size) <= 320
        return make_resp("small")

    provider.text_chat.side_effect = inspect
    assert await describe_multimedia(
        context, {"image_max_edge": 320}, event, store=store
    )
    from pathlib import Path

    assert not await asyncio.to_thread(
        Path(provider.text_chat.call_args.kwargs["image_urls"][0]).exists
    )
    assert PillowImage.open(image).size == (1600, 1200)
    cfg = {**MEDIA_DEFAULTS, "audio_max_seconds": 1}
    with pytest.raises(ValueError, match="duration limit"):
        await prepare_input(str(audio), "audio", cfg)
    assert (await prepare_input(str(audio), "audio", {**cfg, "audio_max_seconds": 2}))[
        1
    ] == 2


@pytest.mark.asyncio
async def test_atomic_global_scope_budgets_and_restart(store, tmp_path):
    cfg = merge_scope_config(
        {"media_daily_requests": 1}, {"media_daily_requests": 10}, "group"
    )
    results = await asyncio.gather(
        *(
            store.media_gate.reserve(cfg, ("group", f"g{i}"), {"image"}, 1, 0)
            for i in range(8)
        )
    )
    assert sum(r[0] is not None for r in results) == 1
    await store.close()
    reopened = MemoryStore(tmp_path / "media.db")
    await reopened.initialize()
    try:
        assert (
            await reopened.media_gate.reserve(cfg, ("group", "new"), {"image"}, 1, 0)
        )[0] is None
        snapshot = await reopened.media_gate.snapshot(cfg)
        assert snapshot["buckets"][0]["usage"][0]["tokens"] == 4096
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_unknown_usage_holds_reservation_until_review(store, setup_media):
    context, provider, event, _, _ = setup_media
    provider.text_chat.return_value = make_resp("no usage")
    cfg = {"media_strict_budget": True}
    assert await describe_multimedia(context, cfg, event, store=store)
    issues = []
    assert not await describe_multimedia(
        context, cfg, event, store=store, problems=issues
    )
    assert "unknown" in issues[0]
    await store.connection.execute("UPDATE media_budget SET created_at=1")
    await store.connection.commit()
    assert (await store.media_gate.reserve(cfg, ("private", "x"), {"image"}, 1, 0))[
        0
    ] is None
    await store.connection.execute("UPDATE media_budget SET acknowledged=1")
    await store.connection.commit()
    assert (await store.media_gate.reserve(cfg, ("private", "x"), {"image"}, 1, 0))[
        0
    ] is not None


@pytest.mark.asyncio
async def test_cache_coalesces_and_isolates_context_scope_model(store, setup_media):
    context, provider, event, _, _ = setup_media
    cfg = {"media_cache_days": 7}
    await asyncio.gather(
        *(
            describe_multimedia(
                context, cfg, event, store=store, scope=("private", "a")
            )
            for _ in range(3)
        )
    )
    assert provider.text_chat.await_count == 1
    await describe_multimedia(context, cfg, event, store=store, scope=("private", "b"))
    assert provider.text_chat.await_count == 2
    event.message_str = "different accompanying text"
    await describe_multimedia(context, cfg, event, store=store, scope=("private", "a"))
    provider.provider_config["model"] = "other"
    await describe_multimedia(context, cfg, event, store=store, scope=("private", "a"))
    assert provider.text_chat.await_count == 4
    await store.delete_scope_memories("private", "a")
    cursor = await store.connection.execute(
        "SELECT COUNT(*) FROM media_cache WHERE scope_key='a'"
    )
    assert (await cursor.fetchone())[0] == 0
    cursor = await store.connection.execute("SELECT COUNT(*) FROM media_budget")
    assert (await cursor.fetchone())[0] == 4


@pytest.mark.asyncio
async def test_manual_selection_preserves_unselected_and_successes(store, setup_media):
    context, provider, event, image, audio = setup_media
    event.message_obj.message = [
        Image.fromFileSystem(image),
        Record.fromFileSystem(audio),
    ]
    cfg = {
        **BASE_CONFIG,
        "image_mode": "manual",
        "audio_mode": "manual",
        "media_cache_days": 7,
    }
    handler = EventHandler(context, cfg, store)
    await handler.on_llm_response(event, make_resp("ok"))
    await handler.media.process_once()
    jobs = (await store.get_processing_status("private", "test:u1"))["items"]
    assert jobs[0]["status"] == "skipped" and jobs[0]["retryable"]
    assert await store.retry_work(jobs[0]["id"], "private", "test:u1", selected=[1])
    await handler.media.process_once()
    assert provider.text_chat.await_count == 1
    image.unlink()
    provider.text_chat.return_value = make_resp("语音内容")
    assert await store.retry_work(jobs[0]["id"], "private", "test:u1", selected=[2])
    await handler.media.process_once()
    assert provider.text_chat.await_count == 2
    assert "image_urls" not in provider.text_chat.call_args.kwargs
    raw = (await store.get_raw_turns("private", "test:u1"))[0][0]["content"]
    assert "红色图片" in raw and "语音内容" in raw


@pytest.mark.asyncio
async def test_budget_pause_does_not_auto_resume_next_day(store, setup_media):
    context, provider, event, _, _ = setup_media
    cfg = {**BASE_CONFIG, "media_daily_tokens": 10}
    handler = EventHandler(context, cfg, store)
    await handler.on_llm_response(event, make_resp("ok"))
    await handler.media.process_once()
    assert (await store.get_processing_status("private", "test:u1"))["items"][0][
        "status"
    ] == "paused"
    cfg["media_daily_tokens"] = 0
    assert not await handler.media.process_once()
    provider.text_chat.assert_not_awaited()


@pytest.mark.parametrize(
    "key,value",
    [
        ("image_mode", "invalid"),
        ("media_daily_requests", True),
        ("image_max_edge", 9000),
        ("media_day_offset", -721),
        ("media_timeout_seconds", 0),
        ("media_daily_tokens", 1.2),
    ],
)
def test_settings_reject_unsafe_values(key, value):
    assert (
        _validate_config_payload({key: value}, GLOBAL_CONFIG_KEYS, GLOBAL_SELECTS)
        is None
    )


def test_media_schema_matches_defaults():
    from pathlib import Path

    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    assert {k: schema[k]["default"] for k in MEDIA_DEFAULTS} == MEDIA_DEFAULTS


@pytest.mark.asyncio
async def test_audio_global_seconds_cannot_be_bypassed_by_scope_override(
    store, setup_media
):
    context, provider, event, _, audio = setup_media
    event.message_obj.message = [Record.fromFileSystem(audio)]
    cfg = merge_scope_config(
        {"audio_daily_seconds": 1}, {"audio_daily_seconds": 0}, "private"
    )
    problems = []
    assert not await describe_multimedia(
        context, cfg, event, store=store, problems=problems
    )
    assert "daily seconds" in problems[0]
    provider.text_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_call_keeps_its_charge_and_cache_empty(store, setup_media):
    context, provider, event, _, _ = setup_media
    entered = asyncio.Event()

    async def wait_forever(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    provider.text_chat.side_effect = wait_forever
    task = asyncio.create_task(
        describe_multimedia(context, {"media_cache_days": 7}, event, store=store)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cursor = await store.connection.execute("SELECT status,tokens FROM media_budget")
    assert tuple(await cursor.fetchone()) == ("unknown", 4096)
    cursor = await store.connection.execute("SELECT COUNT(*) FROM media_cache")
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_cache_capacity_expiry_and_scope_budget(store, setup_media):
    context, provider, event, _, _ = setup_media
    cfg = {"media_cache_days": 1, "media_cache_entries": 1}
    await describe_multimedia(context, cfg, event, store=store)
    event.message_str = "new context"
    await describe_multimedia(context, cfg, event, store=store)
    cursor = await store.connection.execute("SELECT COUNT(*) FROM media_cache")
    assert (await cursor.fetchone())[0] == 1
    await store.connection.execute("UPDATE media_cache SET expires=1")
    await store.connection.commit()
    await describe_multimedia(context, cfg, event, store=store)
    assert provider.text_chat.await_count == 3
    cfg = merge_scope_config(
        {"media_daily_requests": 10}, {"image_daily_count": 1}, "group"
    )
    assert (await store.media_gate.reserve(cfg, ("group", "g"), {"image"}, 1, 0))[0]
    assert (await store.media_gate.reserve(cfg, ("group", "g"), {"image"}, 1, 0))[
        0
    ] is None
    assert (await store.media_gate.reserve(cfg, ("group", "h"), {"image"}, 1, 0))[0]


@pytest.mark.asyncio
async def test_nested_forward_uses_whole_event_limit_and_manual_policy(
    store, setup_media
):
    from astrbot.api.message_components import Node, Nodes, Plain
    from test_forwarding import forward_event

    context, provider, _, image, _ = setup_media
    event = forward_event(
        [
            Nodes(
                nodes=[
                    Node(
                        name="a",
                        uin="1",
                        content=[
                            Plain(text="reference"),
                            Image.fromFileSystem(image),
                            Node(
                                name="b", uin="2", content=[Image.fromFileSystem(image)]
                            ),
                        ],
                    )
                ]
            )
        ]
    )
    cfg = {**BASE_CONFIG, "image_forward_mode": "manual", "image_max_count": 1}
    handler = EventHandler(context, cfg, store)
    await handler.on_group_message(event)
    await handler.media.process_once()
    provider.text_chat.assert_not_awaited()
    job = (await store.get_processing_status("group", "test:g1"))["items"][0]
    assert job["status"] == "skipped" and len(job["attachments"]) == 2
    assert await store.retry_work(job["id"], "group", "test:g1")
    await handler.media.process_once()
    assert provider.text_chat.await_count == 1
    assert len(provider.text_chat.call_args.kwargs["image_urls"]) == 1


@pytest.mark.asyncio
async def test_api_media_review_and_raw_selection_are_scoped(store, setup_media):
    from astrbot.api.web import PluginRequest, bind_request_context
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from core.web_api import WebApi

    class Plugin:
        pass

    plugin = Plugin()
    context, _, event, _, _ = setup_media
    plugin.store, plugin.config, plugin.context = (
        store,
        {**BASE_CONFIG, "image_mode": "manual"},
        context,
    )
    plugin._initialized, plugin._terminating = True, False
    plugin.event_handler = EventHandler(context, plugin.config, store)
    await plugin.event_handler.on_llm_response(event, make_resp("ok"))
    await plugin.event_handler.media.process_once()
    api = WebApi(plugin)
    app = FastAPI()

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await getattr(api, action)()

    reservation, _ = await store.media_gate.reserve(
        {}, ("private", "other"), {"image"}, 1, 0
    )
    await store.media_gate.settle(reservation, None)
    raw_id = (await store.get_raw_turns("private", "test:u1"))[0][0]["id"]
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        result = (
            await client.get(
                "/raw_event",
                params={"scope_type": "private", "scope_key": "test:u1", "id": raw_id},
            )
        ).json()
        assert result["media_job"]["attachments"] == [{"index": 1, "kind": "image"}]
        assert "file" not in result["media_job"]
        assert (
            await client.get(
                "/raw_event",
                params={"scope_type": "private", "scope_key": "other", "id": raw_id},
            )
        ).status_code == 400
        assert (
            await client.post(
                "/review_media_budget",
                json={"scope_type": "private", "scope_key": "test:u1"},
            )
        ).status_code == 200
        cursor = await store.connection.execute(
            "SELECT acknowledged,tokens FROM media_budget WHERE id=?", (reservation,)
        )
        assert tuple(await cursor.fetchone()) == (0, 4096)
        assert (await client.post("/review_media_budget", json={})).status_code == 200
        cursor = await store.connection.execute(
            "SELECT acknowledged,tokens FROM media_budget WHERE id=?", (reservation,)
        )
        assert tuple(await cursor.fetchone()) == (1, 4096)


@pytest.mark.asyncio
async def test_changed_settings_before_dispatch_do_not_spend(
    store, setup_media, monkeypatch
):
    context, provider, event, _, _ = setup_media
    from core import media_inputs

    original = media_inputs.prepare_input

    async def changed(*args):
        result = await original(*args)
        store.config_revision += 1
        return result

    monkeypatch.setattr(media_inputs, "prepare_input", changed)
    problems = []
    assert not await describe_multimedia(
        context, {}, event, store=store, problems=problems
    )
    assert "settings changed" in problems[0]
    provider.text_chat.assert_not_awaited()
    cursor = await store.connection.execute("SELECT COUNT(*) FROM media_budget")
    assert (await cursor.fetchone())[0] == 0
