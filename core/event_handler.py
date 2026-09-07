"""
事件处理入口：把 AstrBot 钩子分发到各功能模块。

群聊被动捕获使用 custom_filter：filter() 执行捕获副作用但始终返回 False，
使 handler 不被激活、不唤醒机器人，从而收到所有群消息却不产生回复。

Text capture is free of LLM calls; supported media is described asynchronously after text capture.
Consolidation uses the stored text for periodic batch extraction.
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest

from .consolidation import ConsolidationScheduler
from .media_processor import MediaProcessor
from .memory_recall import handle_recall
from .memory_writer import (
    handle_group_message,
    handle_group_response,
    handle_private_response,
)
from .scope import resolve_scope
from .storage import MemoryStore


class EventHandler:
    """钩子分发器，由 main.py 注入依赖。"""

    def __init__(self, context, config: dict, store: MemoryStore):
        self.context = context
        self.config = config
        self.store = store
        self.scheduler = ConsolidationScheduler(context, config, store)
        self.media = MediaProcessor(context, config, store)

    # 生命周期
    async def start(self) -> None:
        self.scheduler.start()
        self.media.start()

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.media.stop()

    # 钩子入口
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        async with self.store.transaction():
            scope = resolve_scope(event)
            event.set_extra(
                "memoir_revision",
                await self.store.get_revision(scope.scope_type, scope.scope_key),
            )
            await handle_recall(self.context, self.config, self.store, event, req)

    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        # 私聊：on_llm_response 后把「用户 + 助手」完整一轮落库。
        # 群聊：用户侧由被动捕获覆盖，这里补记机器人自己的回复，
        # 否则群聊巩固时缺失 bot 侧上下文。
        async with self.store.transaction():
            scope = resolve_scope(event)
            expected = event.get_extra("memoir_revision")
            if expected is not None and expected != await self.store.get_revision(
                scope.scope_type, scope.scope_key
            ):
                return
            if event.is_private_chat():
                await handle_private_response(
                    self.context,
                    self.config,
                    self.store,
                    event,
                    resp,
                    processor=self.media,
                )
            else:
                await handle_group_response(
                    self.context, self.config, self.store, event, resp
                )

    async def on_group_message(
        self, event: AstrMessageEvent, *, generation: int | None = None
    ) -> None:
        """Capture group text and supported media without sending a reply.

        Args:
            event: Incoming group message event.
            generation: Scope generation when this capture was queued.
        """
        async with self.store.transaction():
            scope = resolve_scope(event)
            if generation is not None and generation != self.store.generations.get(
                (scope.scope_type, scope.scope_key), 0
            ):
                return
            await handle_group_message(
                self.context, self.config, self.store, event, processor=self.media
            )
