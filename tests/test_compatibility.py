"""Exercise native ownership, native STT ordering and request-level de-duplication."""

import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from astrbot.api.message_components import Image, Node, Plain, Record, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.agent.message import TextPart
from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from test_forwarding import forward_event
from test_pipeline_e2e import BASE_CONFIG, make_event, make_resp

from core.compatibility import native_policy
from core.event_handler import EventHandler
from core.passive_group_capture import (
    PassiveGroupCaptureFilter,
    clear_active_plugin,
    set_active_plugin,
)
from core.storage import MemoryStore
from core.web_api import WebApi


@pytest_asyncio.fixture
async def store(tmp_path):
    db = MemoryStore(tmp_path / "compatibility.db")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def context():
    provider = SimpleNamespace(
        provider_config={"id": "chat", "modalities": ["text", "image", "audio"]},
        text_chat=AsyncMock(return_value=make_resp("Description")),
    )
    return SimpleNamespace(
        get_config=Mock(return_value={}),
        get_using_provider_async=AsyncMock(return_value=provider),
        get_provider_by_id=Mock(return_value=provider),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,vision,runner,group_enabled,caption",
    [
        (True, False, "local", False, False),
        (True, True, "local", False, False),
        (True, False, "dify", False, False),
        (False, True, "local", True, True),
        (False, True, "local", False, True),
        (False, True, "local", True, False),
    ],
)
async def test_policy_uses_real_session_and_native_scope(
    context, private, vision, runner, group_enabled, caption
):
    native = {
        "provider_stt_settings": {"enable": True},
        "provider_settings": {"default_image_caption_provider_id": "caption"},
        "agent_runner": {"runner_type": runner},
        "provider_ltm_settings": {
            "group_icl_enable": group_enabled,
            "image_caption": caption,
            "image_caption_provider_id": "caption",
            "group_message_history_enable": True,
            "active_reply": {"enable": True},
        },
    }
    context.get_config.side_effect = lambda *, umo: (
        native if umo == "correct-session" else {}
    )
    context.get_using_provider_async.return_value.provider_config["modalities"] = (
        ["image"] if vision else ["text"]
    )
    policy = await native_policy(
        context, {}, "correct-session", "private" if private else "group"
    )
    assert policy["audio"] is True
    assert policy["request_image"] == (not vision and runner == "local")
    assert policy["recent"] == (not private and group_enabled)
    assert policy["group_image"] == (not private and group_enabled and caption)
    assert not (await native_policy(context, {}, "another-bot-session", "group"))[
        "audio"
    ]
    context.get_config.assert_any_call(umo="correct-session")
    original_calls = context.get_config.call_count
    disabled = await native_policy(
        context, {"auto_native_compatibility": False}, "correct-session", "group"
    )
    assert not disabled["enabled"] and not disabled["audio"]
    assert context.get_config.call_count == original_calls


@pytest.mark.asyncio
async def test_missing_config_and_unavailable_main_model_do_not_claim_images(context):
    assert not (await native_policy(context, {}, None, "private"))["detected"]
    context.get_config.side_effect = RuntimeError("unavailable")
    assert not (await native_policy(context, {}, "session", "private"))["detected"]
    context.get_config.side_effect = None
    context.get_config.return_value = {
        "provider_stt_settings": {"enable": True},
        "provider_settings": {"default_image_caption_provider_id": "caption"},
    }
    context.get_using_provider_async.side_effect = RuntimeError("provider unavailable")
    policy = await native_policy(context, {}, "session", "private")
    assert policy["audio"] and not policy["request_image"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modalities,caption",
    [([], False), (None, True), ("image", True), (["image"], False)],
)
async def test_native_image_capabilities_match_astrbot_migration_rules(
    context, modalities, caption
):
    context.get_config.return_value = {
        "provider_settings": {"default_image_caption_provider_id": "caption"}
    }
    context.get_using_provider_async.return_value.provider_config["modalities"] = (
        modalities
    )
    assert (await native_policy(context, {}, "session", "private"))[
        "request_image"
    ] == caption


@pytest.mark.asyncio
async def test_event_provider_override_is_used_for_native_image_decision(context):
    context.get_config.return_value = {
        "provider_settings": {"default_image_caption_provider_id": "caption"}
    }
    context.get_using_provider_async.return_value = SimpleNamespace(
        provider_config={"modalities": ["text"]}
    )
    policy = await native_policy(
        context, {}, "session", "private", provider_id="vision-override"
    )
    assert not policy["request_image"]
    context.get_using_provider_async.assert_not_awaited()
    context.get_provider_by_id.assert_called_once_with("vision-override")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,wake,caption,expect_call",
    [
        (True, False, "main", False),
        (False, False, "main", True),
        (False, True, "main", False),
        (False, False, "group", False),
    ],
)
async def test_native_image_ownership_only_blocks_covered_messages(
    store, context, monkeypatch, private, wake, caption, expect_call
):
    context.get_config.return_value = {
        "provider_settings": {
            "default_image_caption_provider_id": "caption" if caption == "main" else ""
        },
        "provider_ltm_settings": {
            "group_icl_enable": caption == "group",
            "image_caption": True,
            "image_caption_provider_id": "caption",
        },
    }
    context.get_using_provider_async.return_value = SimpleNamespace(
        provider_config={"modalities": ["text"]}
    )
    event = make_event("photo", private=private, images=1)
    event.is_at_or_wake_command = wake
    handler = EventHandler(
        context, {**BASE_CONFIG, "background_llm_provider": "vision"}, store
    )
    describe = AsyncMock(return_value="Description")
    monkeypatch.setattr("core.media_processor.describe_multimedia", describe)
    if private:
        await handler.on_llm_response(event, make_resp("Reply"))
    else:
        await handler.on_group_message(event)
    assert await handler.media.process_once() == expect_call
    assert describe.await_count == int(expect_call)
    assert (await store.get_llm_usage(0, int(time.time()) + 1))["totals"]["calls"] == 0


@pytest.mark.asyncio
async def test_queued_work_rechecks_native_settings_before_resolving_media(
    store, context, monkeypatch
):
    event = make_event("", images=1)
    handler = EventHandler(context, BASE_CONFIG, store)
    await handler.on_llm_response(event, make_resp("Reply"))
    context.get_config.return_value = {
        "provider_settings": {"default_image_caption_provider_id": "caption"}
    }
    context.get_using_provider_async.return_value.provider_config["modalities"] = [
        "text"
    ]
    convert = AsyncMock(side_effect=AssertionError("native-owned image accessed"))
    monkeypatch.setattr(Image, "convert_to_file_path", convert)
    assert await handler.media.process_once()
    convert.assert_not_awaited()
    assert (await store.get_llm_usage(0, int(time.time()) + 1))["totals"]["calls"] == 0
    assert not (await store.get_processing_status("private", "test:u1"))["failures"]


@pytest.mark.asyncio
async def test_forward_media_is_not_blocked_by_native_plain_message_settings(
    store, context, monkeypatch
):
    context.get_config.return_value = {
        "provider_stt_settings": {"enable": True},
        "provider_ltm_settings": {
            "group_icl_enable": True,
            "image_caption": True,
            "image_caption_provider_id": "caption",
        },
    }
    describe = AsyncMock(return_value="Forwarded picture and voice")
    monkeypatch.setattr("core.media_processor.describe_multimedia", describe)
    handler = EventHandler(context, BASE_CONFIG, store)
    await handler.on_group_message(
        forward_event(
            [Node(content=[Image(file="image.png"), Record(file="audio.wav")])]
        )
    )
    assert await handler.media.process_once()
    describe.assert_awaited_once()
    parts = describe.call_args.args[2].message_obj.message
    assert isinstance(parts[0], Image) and isinstance(parts[1], Record)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_native_stt_captures_after_preprocessing_without_plugin_call_or_reply(
    store, context, tmp_path, success
):
    audio = tmp_path / "voice.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 160)
    native_config = {
        "provider_stt_settings": {"enable": True},
        "provider_settings": {"enable": True},
    }
    context.get_config.return_value = native_config
    stt = SimpleNamespace(
        get_text=AsyncMock(return_value="Original voice text" if success else "")
    )
    context.get_using_stt_provider_async = AsyncMock(return_value=stt)
    event = make_event("", private=False)
    event.message_obj.message = [Record.fromFileSystem(audio)]
    handler = EventHandler(context, BASE_CONFIG, store)

    class Plugin:
        pass

    plugin = Plugin()
    plugin.config, plugin.store = BASE_CONFIG, store
    plugin.submit_group_capture = Mock()
    set_active_plugin(plugin)
    try:
        assert PassiveGroupCaptureFilter(False).filter(event, native_config)
        plugin.submit_group_capture.assert_not_called()
        assert (await store.get_raw_turns("group", "test:g1"))[1] == 0
        preprocess = PreProcessStage()
        await preprocess.initialize(
            SimpleNamespace(
                astrbot_config=native_config,
                plugin_manager=SimpleNamespace(context=context),
            )
        )
        await preprocess.process(event)
        stt.get_text.assert_awaited_once()

        # Run the actual ProcessStage gating, with the activated capture handler.
        class CaptureStage:
            async def process(self, current):
                yield await handler.on_group_message(
                    current, generation=current.get_extra("memoir_capture_generation")
                )

        process = ProcessStage()
        process.ctx = SimpleNamespace(astrbot_config=native_config)
        process.star_request_sub_stage = CaptureStage()
        process.agent_sub_stage = SimpleNamespace(
            process=Mock(side_effect=AssertionError("Unexpected main LLM reply"))
        )
        event.set_extra("activated_handlers", ["capture"])
        async for _ in process.process(event):
            pass
        rows, count = await store.get_raw_turns("group", "test:g1")
        assert count == 1
        assert rows[0]["content"] == ("Original voice text" if success else "[语音]")
        assert not await handler.media.process_once()
        context.get_provider_by_id.assert_not_called()
        assert not event.is_at_or_wake_command
    finally:
        clear_active_plugin(plugin)


@pytest.mark.asyncio
async def test_new_media_resumes_when_native_feature_is_disabled(
    store, context, monkeypatch
):
    native = {
        "provider_ltm_settings": {
            "group_icl_enable": True,
            "image_caption": True,
            "image_caption_provider_id": "caption",
        }
    }
    context.get_config.return_value = native
    describe = AsyncMock(return_value="Image description")
    monkeypatch.setattr("core.media_processor.describe_multimedia", describe)
    handler = EventHandler(context, BASE_CONFIG, store)
    await handler.on_group_message(make_event("first", private=False, images=1))
    assert not await handler.media.process_once()
    native["provider_ltm_settings"]["image_caption"] = False
    await handler.on_group_message(make_event("second", private=False, images=1))
    assert await handler.media.process_once()
    describe.assert_awaited_once()
    rows, _ = await store.get_raw_turns("group", "test:g1")
    assert (
        next(row for row in rows if "first" in row["content"])["content"]
        == "first [图片]"
    )
    assert (
        "Image description"
        in next(row for row in rows if "second" in row["content"])["content"]
    )


@pytest.mark.asyncio
async def test_main_caption_prefix_and_nonconversation_requests_do_not_claim_images(
    store, context, monkeypatch
):
    context.get_config.return_value = {
        "provider_settings": {
            "default_image_caption_provider_id": "caption",
            "wake_prefix": "/ask",
        }
    }
    context.get_using_provider_async.return_value = SimpleNamespace(
        provider_config={"modalities": ["text"]}
    )
    assert not (
        await native_policy(context, {}, "session", "group", message_text="photo")
    )["request_image"]
    assert (
        await native_policy(context, {}, "session", "group", message_text="/ask photo")
    )["request_image"]
    event = make_event("/ask photo", images=1)
    event.set_extra("provider_request", ProviderRequest(prompt="/ask photo"))
    handler = EventHandler(context, BASE_CONFIG, store)
    describe = AsyncMock(return_value="Description")
    monkeypatch.setattr("core.media_processor.describe_multimedia", describe)
    await handler.on_llm_response(event, make_resp("Reply"))
    assert await handler.media.process_once()
    describe.assert_awaited_once()


@pytest.mark.asyncio
async def test_stt_direct_text_does_not_capture_quoted_transcript(store, context):
    event = make_event("User text plus quoted STT", private=False)
    event.message_obj.message = [
        Plain(text="Direct speech"),
        Reply(id="quote", chain=[Plain(text="Quoted speech")]),
    ]
    event.set_extra("memoir_native_stt", True)
    await EventHandler(context, BASE_CONFIG, store).on_group_message(event)
    rows, _ = await store.get_raw_turns("group", "test:g1")
    assert rows[0]["content"] == "Direct speech"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_enabled", [False, True])
async def test_group_context_owns_recent_injection_and_preserves_long_term(
    store, context, native_enabled
):
    context.get_config.return_value = {
        "provider_ltm_settings": {
            "group_icl_enable": native_enabled,
            "group_message_history_enable": True,
        }
    }
    await store.insert_raw_turn(
        scope_type="group", scope_key="test:g1", content="独立的近期消息"
    )
    await store.insert_memory(
        scope_type="group",
        scope_key="test:g1",
        memory_type="semantic",
        content="用户喜欢研究摄影和构图",
        importance=5,
    )
    req = ProviderRequest(prompt="你好")
    if native_enabled:
        req.extra_user_content_parts.append(
            TextPart(text="[小张/10:00:00]: 独立的近期消息")
        )
    before = len(req.extra_user_content_parts)
    store.get_recent_raw = AsyncMock(wraps=store.get_recent_raw)
    await EventHandler(context, BASE_CONFIG, store).on_llm_request(
        make_event("你好", private=False), req
    )
    text = "".join(part.text for part in req.extra_user_content_parts[before:])
    assert "用户喜欢研究摄影和构图" in text
    assert ("独立的近期消息" in text) == (not native_enabled)
    assert store.get_recent_raw.await_count == (1 if native_enabled else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["group_block", "history", "current"])
async def test_group_raw_recall_deduplicates_actual_request_content(
    store, context, source
):
    text = "我最近正在学习自然光人像摄影技巧"
    context.get_config.return_value = {
        "provider_ltm_settings": {"group_icl_enable": True}
    }
    await store.insert_raw_turn(scope_type="group", scope_key="test:g1", content=text)
    req = ProviderRequest(prompt=text if source == "current" else "自然光摄影技巧")
    if source == "group_block":
        req.extra_user_content_parts.append(
            TextPart(
                text=f"<system_reminder>\n[小张/10:00:00]: {text}\n</system_reminder>"
            )
        )
    elif source == "history":
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    before = len(req.extra_user_content_parts)
    await EventHandler(context, BASE_CONFIG, store).on_llm_request(
        make_event(req.prompt, private=False), req
    )
    assert text not in "".join(
        part.text for part in req.extra_user_content_parts[before:]
    )


@pytest.mark.asyncio
async def test_scope_status_and_switch_preserve_model_configuration(store, context):
    class Plugin:
        pass

    plugin = Plugin()
    plugin.store, plugin.context = store, context
    plugin.config = {
        **BASE_CONFIG,
        "image_llm_provider": "keep-image",
        "audio_llm_provider": "keep-audio",
    }
    plugin._initialized, plugin._terminating = True, False
    context.get_config.return_value = {
        "provider_stt_settings": {"enable": True},
        "provider_ltm_settings": {"group_icl_enable": True},
    }
    await store.insert_raw_turn(
        scope_type="group",
        scope_key="test:g1",
        content="One message",
        umo="actual-session",
    )
    api = WebApi(plugin)
    app = FastAPI()

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        with bind_request_context(PluginRequest(request)):
            return await getattr(api, action)()

    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        scope = {"scope_type": "group", "scope_key": "test:g1"}
        data = (await client.get("/get_scope_config", params=scope)).json()
        assert (
            data["compatibility"]["audio"]
            and data["effective"]["recall_recent_turns"] == 0
        )
        context.get_config.assert_called_with(umo="actual-session")
        assert (
            await client.post(
                "/update_global_config", json={"auto_native_compatibility": "false"}
            )
        ).status_code == 400
        assert (
            await client.post(
                "/update_global_config", json={"auto_native_compatibility": False}
            )
        ).status_code == 200
        data = (await client.get("/get_scope_config", params=scope)).json()
        assert not data["compatibility"]["enabled"]
        assert (
            data["effective"]["recall_recent_turns"]
            == BASE_CONFIG["recall_recent_turns"]
        )
        assert (
            plugin.config["image_llm_provider"] == "keep-image"
            and plugin.config["audio_llm_provider"] == "keep-audio"
        )
