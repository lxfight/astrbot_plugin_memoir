"""Verify exact usage accounting, lifecycle outcomes, filtering and model routing."""

import asyncio
import json
import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from astrbot.api.message_components import Image, Record
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.provider.entities import TokenUsage
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from test_pipeline_e2e import BASE_CONFIG, make_event, make_resp

from core.consolidation import _consolidate_scope
from core.llm_helper import describe_multimedia
from core.storage import MemoryStore
from core.usage import tracked_call
from core.web_api import WebApi


@pytest_asyncio.fixture
async def store(tmp_path):
    db = MemoryStore(tmp_path / "usage.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage,expected",
    [
        (TokenUsage(input_other=123, input_cached=40, output=9), (123, 40, 9)),
        (TokenUsage(), (0, 0, 0)),
        (None, (None, None, None)),
        (SimpleNamespace(input_other=-1, input_cached=0, output=2), (None,) * 3),
        (SimpleNamespace(input_other=True, input_cached=0, output=2), (None,) * 3),
        (SimpleNamespace(input_other=2**53, input_cached=0, output=2), (None,) * 3),
    ],
)
async def test_exact_usage_without_content_or_estimates(store, usage, expected):
    response = make_resp("PRIVATE RESPONSE")
    response.usage = usage
    provider = SimpleNamespace(
        provider_config={"id": "vision", "api_key": "SECRET KEY"},
        get_model=lambda: "actual-model",
    )
    assert (
        await tracked_call(
            AsyncMock(return_value=response),
            store=store,
            scope=("private", "p:1"),
            purpose="media_image",
            provider=provider,
        )
        is response
    )
    result = await store.get_llm_usage(0, int(time.time()) + 1)
    row = result["items"][0]
    assert (
        tuple(row[key] for key in ("input_other", "input_cached", "output")) == expected
    )
    assert result["totals"]["reported_calls"] == int(
        usage is not None and expected[0] is not None
    )
    assert row["model"] == "actual-model" and row["provider_id"] == "vision"
    assert row["status"] == "complete" and row["duration_ms"] >= 0
    assert "PRIVATE RESPONSE" not in json.dumps(result)
    assert "SECRET KEY" not in json.dumps(result)


@pytest.mark.asyncio
async def test_error_cancellation_and_restart_are_visible(store):
    with pytest.raises(RuntimeError):
        await tracked_call(
            AsyncMock(side_effect=RuntimeError("SECRET")),
            store=store,
            purpose="consolidation",
        )
    started = asyncio.Event()

    async def pending():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        tracked_call(pending, store=store, purpose="media_audio")
    )
    await started.wait()
    running = await store.get_llm_usage(0, int(time.time()) + 1)
    assert running["items"][0]["status"] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await store.start_llm_usage("group", "p:1", "media_image", "vision", "model")
    await store.close()
    await store.initialize()
    result = await store.get_llm_usage(0, int(time.time()) + 1)
    assert [row["status"] for row in result["items"]] == [
        "interrupted",
        "cancelled",
        "error",
    ]
    assert result["totals"]["failed_calls"] == 3
    assert result["totals"]["reported_calls"] == 0
    assert "SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_consolidation_retry_is_counted_even_when_output_is_rejected(store):
    response = make_resp("not JSON")
    response.usage = TokenUsage(input_other=12, input_cached=8, output=2)
    provider = SimpleNamespace(provider_config={"id": "cheap", "model": "small"})
    context = SimpleNamespace(
        get_provider_by_id=lambda _: provider,
        llm_generate=AsyncMock(return_value=response),
    )
    await store.insert_raw_turn(scope_type="private", scope_key="p:1", content="A fact")
    await _consolidate_scope(
        context,
        {**BASE_CONFIG, "background_llm_provider": "cheap"},
        store,
        "private",
        "p:1",
    )
    result = await store.get_llm_usage(0, int(time.time()) + 1)
    assert context.llm_generate.await_count == result["totals"]["calls"] == 2
    assert result["totals"]["input_other"] == 24
    assert result["totals"]["input_cached"] == 16
    assert result["totals"]["output"] == 4
    assert await store.get_scope_memories("private", "p:1") == []


@pytest.mark.asyncio
async def test_media_provider_error_keeps_usage_and_other_modality(
    store, tmp_path, monkeypatch
):
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        Record, "convert_to_file_path", AsyncMock(return_value=str(image))
    )
    failure = make_resp("Must not become a memory")
    failure.role = "err"
    failure.usage = TokenUsage(input_other=4, output=1)
    success = make_resp("Audio transcription")
    success.usage = TokenUsage(input_other=10, output=5)
    vision = SimpleNamespace(
        provider_config={"id": "vision", "modalities": ["image"]},
        text_chat=AsyncMock(return_value=failure),
    )
    voice = SimpleNamespace(
        provider_config={"id": "voice", "modalities": ["audio"]},
        text_chat=AsyncMock(return_value=success),
    )
    context = SimpleNamespace(get_provider_by_id={"vision": vision, "voice": voice}.get)
    event = make_event("")
    event.message_obj.message = [Image.fromFileSystem(image), Record(file=str(image))]
    problems = []
    text = await describe_multimedia(
        context,
        {"image_llm_provider": "vision", "audio_llm_provider": "voice"},
        event,
        store=store,
        problems=problems,
    )
    assert text == "Audio transcription" and problems == [
        "Media model returned an error response"
    ]
    result = await store.get_llm_usage(0, int(time.time()) + 1)
    assert result["totals"]["calls"] == result["totals"]["reported_calls"] == 2
    assert result["totals"]["failed_calls"] == 1
    assert result["totals"]["input_other"] == 14
    assert result["totals"]["output"] == 6


@pytest.mark.asyncio
async def test_cursor_filters_empty_and_local_day_boundaries(store):
    empty = await store.get_llm_usage(0, 2**31)
    assert set(empty["totals"].values()) == {0}
    boundary = 86400 - 480 * 60
    for index in range(55):
        record = await store.start_llm_usage(
            "private", "p:1", "media_image", "vision", "m"
        )
        await store.finish_llm_usage(record, "complete", (10, 5, 2), 10, "")
        await store.connection.execute(
            "UPDATE llm_usage SET created_at=? WHERE id=?",
            (boundary - 1 if index == 0 else boundary, record),
        )
    await store.connection.commit()
    await store.start_llm_usage("group", "p:1", "media_audio", "audio", "m")
    first = await store.get_llm_usage(
        0, 2**31, 480, ("private", "p:1"), "vision", "media_image"
    )
    second = await store.get_llm_usage(
        0, 2**31, 480, ("private", "p:1"), "vision", "media_image", first["next_cursor"]
    )
    assert first["totals"] == second["totals"]
    assert first["totals"]["calls"] == 55
    assert [row["calls"] for row in first["daily"]] == [1, 54]
    assert [row["day"] for row in first["daily"]] == ["1970-01-01", "1970-01-02"]
    assert len(first["items"]) == 50 and len(second["items"]) == 5
    assert not {row["id"] for row in first["items"]} & {
        row["id"] for row in second["items"]
    }
    assert second["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True, "session"])
async def test_media_models_route_separately_or_share_one_call(
    store, tmp_path, separate
):
    image = tmp_path / "photo.png"
    audio = tmp_path / "audio.wav"
    image.write_bytes(b"image")
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 160)
    response = make_resp("Description")
    response.usage = TokenUsage(input_other=10, input_cached=3, output=2)
    vision = SimpleNamespace(
        provider_config={"id": "vision", "modalities": ["image", "audio"]},
        text_chat=AsyncMock(return_value=response),
    )
    voice = SimpleNamespace(
        provider_config={"id": "voice", "modalities": ["audio"]},
        text_chat=AsyncMock(return_value=response),
    )
    context = SimpleNamespace(
        get_provider_by_id=Mock(side_effect={"vision": vision, "voice": voice}.get),
        get_using_provider_async=AsyncMock(return_value=vision),
    )
    event = make_event("")
    event.message_obj.message = [
        Image.fromFileSystem(image),
        Record.fromFileSystem(audio),
    ]
    config = {
        "background_llm_provider": "" if separate == "session" else "vision",
        "image_llm_provider": "vision" if separate == "session" else "",
        "audio_llm_provider": "voice" if separate is True else "",
    }
    assert await describe_multimedia(
        context, config, event, store=store, scope=("private", "p:1")
    )
    result = await store.get_llm_usage(0, int(time.time()) + 1)
    assert result["totals"]["calls"] == (2 if separate is True else 1)
    assert vision.text_chat.call_args.kwargs["image_urls"] == [str(image)]
    if separate is True:
        assert "audio_urls" not in vision.text_chat.call_args.kwargs
        assert voice.text_chat.call_args.kwargs["audio_urls"] == [str(audio)]
        assert {row["purpose"] for row in result["items"]} == {
            "media_image",
            "media_audio",
        }
    else:
        assert vision.text_chat.call_args.kwargs["audio_urls"] == [str(audio)]
        assert result["items"][0]["purpose"] == "media_mixed"
    if separate == "session":
        context.get_using_provider_async.assert_awaited_once_with(
            umo=event.unified_msg_origin
        )
    else:
        context.get_using_provider_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_api_validation_and_provider_config(store):
    class Plugin:
        pass

    plugin = Plugin()
    plugin.store, plugin.config = store, {}
    plugin._initialized, plugin._terminating = True, False
    api = WebApi(plugin)
    app = FastAPI()

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await getattr(api, action)()

    await store.start_llm_usage("private", "p:1", "media_image", "vision", "m")
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        for params in (
            {"days": "bad"},
            {"days": 2},
            {"offset": 841},
            {"before": -1},
            {"before": 2**63},
            {"scope_key": "p:1"},
        ):
            assert (await client.get("/usage", params=params)).status_code == 400
        response = await client.get(
            "/usage",
            params={
                "offset": 480,
                "scope_type": "private",
                "scope_key": "p:1",
                "provider": "vision",
            },
        )
        data = response.json()
        assert data["totals"]["calls"] == 1
        assert (data["since"] + 480 * 60) % 86400 == 0
        assert (await client.get("/usage", params={"provider": "other"})).json()[
            "totals"
        ]["calls"] == 0
        response = await client.post(
            "/update_global_config",
            json={"image_llm_provider": "vision", "audio_llm_provider": "voice"},
        )
        assert response.status_code == 200
        assert plugin.config == {
            "image_llm_provider": "vision",
            "audio_llm_provider": "voice",
        }
        plugin._terminating = True
        assert (await client.get("/usage")).status_code == 400
