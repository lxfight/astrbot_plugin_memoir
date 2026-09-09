"""Account for plugin requests without retaining prompts or media references."""

import asyncio
import time

from astrbot.api import logger


async def tracked_call(
    invoke, *, store=None, scope=("", ""), purpose, provider_id="", provider=None
):
    """Run one provider request and persist its standardized AstrBot token usage.

    Args:
        invoke: Zero-argument callable returning the provider awaitable.
        store: Optional persistent memory store.
        scope: Conversation type/key pair.
        purpose: Plugin task category; mixed media is not artificially allocated.
        provider_id: Configured provider identity.
        provider: Resolved AstrBot provider for model metadata.

    Returns:
        The original provider response.

    Raises:
        BaseException: The original provider failure or cancellation.
    """
    config = getattr(provider, "provider_config", {}) or {}
    model = config.get("model", "")
    try:
        if callable(getattr(provider, "get_model", None)):
            model = provider.get_model()
    except Exception:
        logger.warning("[Memoir] Provider model metadata unavailable")
    record_id = None
    if store:
        record_id = await store.start_llm_usage(
            *scope,
            purpose,
            str(provider_id or config.get("id", "session")),
            str(model or "unknown"),
        )
    started = time.monotonic()
    status, error_type, counts = "error", "", (None, None, None)
    try:
        response = await invoke()
        status = "error" if getattr(response, "role", "") == "err" else "complete"
        usage = getattr(response, "usage", None)
        if usage is not None:
            values = tuple(
                getattr(usage, key, None)
                for key in ("input_other", "input_cached", "output")
            )
            if all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value < 2**53
                for value in values
            ):
                counts = values
        return response
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        error_type = type(exc).__name__
        raise
    finally:
        if record_id is not None:
            try:
                await store.finish_llm_usage(
                    record_id,
                    status,
                    counts,
                    round((time.monotonic() - started) * 1000),
                    error_type,
                )
            except Exception:
                logger.exception("[Memoir] Failed to finalize usage ledger")
