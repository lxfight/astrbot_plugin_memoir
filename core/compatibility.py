"""Resolve native ownership without modifying AstrBot settings or providers."""

import asyncio


async def native_policy(
    context,
    config,
    umo,
    scope_type,
    *,
    check_request_image=True,
    provider_id="",
    message_text=None,
):
    """Read effective native settings for capture, queued work, recall and UI.

    Args:
        context: AstrBot context exposing session configuration and providers.
        config: Plugin configuration; the compatibility switch defaults on.
        umo: Actual AstrBot session identity, or None for an unknown session.
        scope_type: Private or group conversation.
        check_request_image: Resolve main-provider image capabilities when needed.
        provider_id: Optional per-event native provider override.
        message_text: Event text for the native LLM wake prefix; None for UI preview.

    Returns:
        JSON-safe ownership flags. Request image ownership only applies when
        this event triggers the native main agent, never to nested forwards.
    """
    result = {
        "enabled": bool(config.get("auto_native_compatibility", True)),
        "detected": False,
        "audio": False,
        "group_image": False,
        "request_image": False,
        "recent": False,
    }
    if not result["enabled"]:
        return result
    if not umo or not callable(getattr(context, "get_config", None)):
        return result
    try:
        native = context.get_config(umo=umo)
        if not isinstance(native, dict):
            return result
        stt = native.get("provider_stt_settings") or {}
        group = native.get("provider_ltm_settings") or {}
        provider_settings = native.get("provider_settings") or {}
        runner = native.get("agent_runner") or {}
        if not all(
            isinstance(value, dict) for value in (stt, group, provider_settings, runner)
        ):
            return result
        result["detected"] = True
        result["audio"] = bool(stt.get("enable", False))
        result["recent"] = scope_type == "group" and bool(
            group.get("group_icl_enable", False)
        )
        result["group_image"] = bool(
            result["recent"]
            and group.get("image_caption", False)
            and group.get("image_caption_provider_id")
        )
        if (
            check_request_image
            and provider_settings.get("enable", True)
            and provider_settings.get("default_image_caption_provider_id")
            and runner.get("runner_type", "local") == "local"
            and (
                message_text is None
                or message_text.startswith(provider_settings.get("wake_prefix") or "")
            )
        ):
            provider = (
                context.get_provider_by_id(provider_id)
                if provider_id
                else await asyncio.wait_for(
                    context.get_using_provider_async(umo=umo), 5
                )
            )
            if provider is not None:
                modalities = provider.provider_config.get("modalities")
                # Match AstrBot's migrated empty-list capability semantics.
                supports_image = modalities == [] or (
                    isinstance(modalities, list) and "image" in modalities
                )
                result["request_image"] = not supports_image
    except Exception:
        # Unknown provider capabilities do not imply native image ownership.
        result["request_image"] = False
    return result
