"""
事件处理入口：把 AstrBot 钩子分发到各功能模块。

群聊被动捕获使用 custom_filter：filter() 执行编码副作用但始终返回 False，
使 handler 不被激活、不唤醒机器人，从而收到所有群消息却不产生回复。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest

from .consolidation import ConsolidationScheduler
from .memory_recall import handle_recall
from .memory_writer import handle_group_message, handle_private_response
from .storage import MemoryStore


class EventHandler:
    """钩子分发器，由 main.py 注入依赖。"""

    def __init__(self, context, config: dict, store: MemoryStore):
        self.context = context
        self.config = config
        self.store = store
        self.scheduler = ConsolidationScheduler(context, config, store)

    # 生命周期
    async def start(self) -> None:
        self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()

    # 钩子入口
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        await handle_recall(self.context, self.config, self.store, event, req)

    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        # 群聊编码完全交给被动捕获 on_group_message（每条用户消息即时编码）。
        # 这里只在私聊编码：私聊没有被动捕获，需要在 LLM 响应后把
        # 「用户 + 助手」完整一轮一起编码。
        if event.is_private_chat():
            await handle_private_response(self.context, self.config, self.store, event, resp)

    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """被动捕获的群消息：仅做编码，不产生回复"""
        await handle_group_message(self.context, self.config, self.store, event)
