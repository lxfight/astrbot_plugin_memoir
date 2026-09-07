"""Verify media capture, capability gating, privacy, and memory reuse."""

import asyncio
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.message_components import File, Image, Record, Video
from astrbot.api.provider import ProviderRequest
from test_pipeline_e2e import BASE_CONFIG, injected_text, make_event, make_resp

from core import consolidation as consolidation_module
from core.consolidation import run_consolidation_pass
from core.event_handler import EventHandler
from core.llm_helper import describe_multimedia
from core.storage import MemoryStore


@pytest.fixture
def media_context(monkeypatch):
    """Build a provider whose actual requests can be inspected."""
    monkeypatch.setattr(
        "core.llm_helper.Path",
        lambda path: SimpleNamespace(stat=lambda: SimpleNamespace(st_size=100)),
    )
    provider = SimpleNamespace(
        provider_config={"modalities": ["text", "image", "audio"]},
        text_chat=AsyncMock(
            return_value=make_resp("图片1：一只柴犬。语音1：狗叫小福。")
        ),
    )
    return SimpleNamespace(
        get_provider_by_id=Mock(return_value=provider),
        get_using_provider_async=AsyncMock(return_value=provider),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
async def test_media_capture_consolidation_and_recall(
    media_context, tmp_path, monkeypatch, private
):
    image_path = tmp_path / "photo with spaces.png"
    audio_path = tmp_path / "voice.wav"
    image_path.write_bytes(b"image fixture")
    with audio_path.open("wb") as audio_file, wave.open(audio_file, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 160)
    event = make_event("", private=private)
    event.message_obj.message = [
        Image.fromFileSystem(image_path),
        Record.fromFileSystem(audio_path),
    ]
    config = dict(
        BASE_CONFIG,
        background_llm_provider="vision",
        consolidation_count_threshold_private=1,
        consolidation_count_threshold_group=1,
    )
    store = MemoryStore(":memory:")
    await store.initialize()
    try:
        handler = EventHandler(media_context, config, store)
        if private:
            await handler.on_llm_response(event, make_resp("好可爱"))
        else:
            await handler.on_group_message(event)
        await handler.media.process_once()
        scope_type, scope_key = (
            ("private", "test:u1") if private else ("group", "test:g1")
        )
        turns, total = await store.get_raw_turns(scope_type, scope_key)
        assert total == 1
        assert "[图片] [语音] [多媒体解析]" in turns[0]["content"]
        assert "狗叫小福" in turns[0]["content"]
        assert str(tmp_path) not in turns[0]["content"]
        media_context.get_provider_by_id.assert_called_once_with("vision")
        media_context.get_using_provider_async.assert_not_awaited()
        request = (
            media_context.get_provider_by_id.return_value.text_chat.call_args.kwargs
        )
        assert request["image_urls"] == [str(image_path)]
        assert request["audio_urls"] == [str(audio_path)]

        llm = AsyncMock(
            return_value=json.dumps(
                {
                    "semantic_ops": [
                        {
                            "action": "insert",
                            "content": "对话中展示了一只名叫小福的柴犬",
                            "key": "pet:xiaofu",
                            "subject_id": "u1" if not private else None,
                            "tags": "小福,柴犬",
                            "importance": 4,
                        }
                    ]
                }
            )
        )
        monkeypatch.setattr(consolidation_module, "call_background_llm", llm)
        assert await run_consolidation_pass(media_context, config, store) == 1
        assert "狗叫小福" in llm.call_args.kwargs["prompt"]
        req = ProviderRequest(prompt="小福")
        await handler.on_llm_request(make_event("小福", private=private), req)
        assert "小福" in injected_text(req)
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("modalities", [None, [], ["text"], "image"])
async def test_unknown_or_text_only_model_does_not_resolve_media(
    media_context, monkeypatch, modalities
):
    provider = media_context.get_provider_by_id.return_value
    provider.provider_config["modalities"] = modalities
    convert = AsyncMock(side_effect=AssertionError("unsupported media was accessed"))
    monkeypatch.setattr(Image, "convert_to_file_path", convert)
    event = make_event("图片", images=1)
    assert await describe_multimedia(media_context, {}, event) == ""
    provider.text_chat.assert_not_awaited()
    convert.assert_not_awaited()
    media_context.get_using_provider_async.assert_awaited_once_with(
        umo=event.unified_msg_origin
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability, key", [("image", "image_urls"), ("audio", "audio_urls")]
)
async def test_only_supported_parts_are_sent(
    media_context, monkeypatch, capability, key
):
    provider = media_context.get_provider_by_id.return_value
    provider.provider_config["modalities"] = [capability]
    image = AsyncMock(return_value="photo.png")
    audio = AsyncMock(return_value="voice.wav")
    monkeypatch.setattr(Image, "convert_to_file_path", image)
    monkeypatch.setattr(Record, "convert_to_file_path", audio)
    event = make_event("")
    event.message_obj.message = [Image(file="photo.png"), Record(file="voice.wav")]
    assert await describe_multimedia(media_context, {}, event)
    request = provider.text_chat.call_args.kwargs
    assert key in request
    assert ("audio_urls" if capability == "image" else "image_urls") not in request
    (audio if capability == "image" else image).assert_not_awaited()


@pytest.mark.asyncio
async def test_broken_attachment_does_not_discard_other_media(
    media_context, monkeypatch
):
    monkeypatch.setattr(
        Image, "convert_to_file_path", AsyncMock(side_effect=[ValueError(), "ok.png"])
    )
    event = make_event("", images=2)
    assert await describe_multimedia(media_context, {}, event)
    request = media_context.get_provider_by_id.return_value.text_chat.call_args.kwargs
    assert request["image_urls"] == ["ok.png"]
    assert "消息段3" in request["prompt"]
    assert "消息段2" not in request["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("unsupported model"), asyncio.TimeoutError()]
)
async def test_failed_description_preserves_raw_turn(media_context, monkeypatch, error):
    monkeypatch.setattr(
        Image, "convert_to_file_path", AsyncMock(return_value="photo.png")
    )
    media_context.get_provider_by_id.return_value.text_chat.side_effect = error
    store = MemoryStore(":memory:")
    await store.initialize()
    try:
        await EventHandler(media_context, BASE_CONFIG, store).on_group_message(
            make_event("今天的照片", private=False, images=1)
        )
        turns, total = await store.get_raw_turns("group", "test:g1")
        assert total == 1
        assert turns[0]["content"] == "今天的照片 [图片]"
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config, text",
    [
        ({"enable_group_memory": False}, "照片"),
        ({"group_capture_ignored_users": ["u1"]}, "照片"),
        ({"group_capture_ignored_keywords": ["秘密"]}, "秘密"),
        ({}, "/memoir"),
    ],
)
async def test_filtered_messages_never_reach_provider(media_context, config, text):
    store = MemoryStore(":memory:")
    await store.initialize()
    try:
        handler = EventHandler(media_context, dict(BASE_CONFIG, **config), store)
        await handler.on_group_message(make_event(text, private=False, images=1))
        assert (await store.get_raw_turns("group", "test:g1"))[1] == 0
        media_context.get_using_provider_async.assert_not_awaited()
        media_context.get_provider_by_id.assert_not_called()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recognized_keyword_is_filtered(media_context, monkeypatch):
    monkeypatch.setattr(
        Image, "convert_to_file_path", AsyncMock(return_value="photo.png")
    )
    store = MemoryStore(":memory:")
    await store.initialize()
    try:
        handler = EventHandler(
            media_context,
            dict(BASE_CONFIG, group_capture_ignored_keywords=["小福"]),
            store,
        )
        await handler.on_group_message(make_event("", private=False, images=1))
        await handler.media.process_once()
        assert (await store.get_raw_turns("group", "test:g1"))[1] == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_text_video_and_file_messages_skip_model(media_context):
    store = MemoryStore(":memory:")
    await store.initialize()
    try:
        handler = EventHandler(media_context, BASE_CONFIG, store)
        await handler.on_group_message(make_event("文字", private=False))
        event = make_event("", private=False)
        event.message_obj.message = [Video(file="video.mp4"), File(name="document.pdf")]
        await handler.on_group_message(event)
        turns, total = await store.get_raw_turns("group", "test:g1")
        assert total == 2
        assert {turn["content"] for turn in turns} == {"文字", "[视频] [文件]"}
        media_context.get_using_provider_async.assert_not_awaited()
    finally:
        await store.close()
