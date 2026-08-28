"""
astrbot_plugin_memoir 主入口。

模拟人脑记忆闭环：编码 -> 巩固 -> 遗忘 -> 召回 -> 洞察。
私聊与群聊采用不同策略，不使用向量检索/RAG。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config import AstrBotConfig

from .core.event_handler import EventHandler
from .core.passive_group_capture import PassiveGroupCaptureFilter, set_active_plugin
from .core.storage import MemoryStore


class MemoirPlugin(Star):
    """记忆闭环插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config: dict[str, Any] = config

        # 插件数据目录：data/plugin_data/astrbot_plugin_memoir/
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_memoir"))
        self.db_path = str(data_dir / "memoir.db")

        self.store = MemoryStore(self.db_path)
        self.event_handler: EventHandler | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._initialized = False
        self._terminating = False

        set_active_plugin(self)

        # 非阻塞初始化：Provider 可能尚未就绪，异步启动避免阻塞 AstrBot 启动
        self._track_task(asyncio.create_task(self._initialize_async()))

    def _track_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _initialize_async(self) -> None:
        try:
            await self.store.initialize()
            self.event_handler = EventHandler(self.context, self.config, self.store)
            await self.event_handler.start()
            self._initialized = True
            logger.info("[Memoir] 初始化完成，记忆闭环已启动")
        except Exception as exc:
            logger.error(f"[Memoir] 初始化失败: {exc}", exc_info=True)

    async def initialize(self) -> None:
        """AstrBot 生命周期钩子。__init__ 已异步启动初始化，这里只做兜底等待。"""
        if not self._initialized:
            for _ in range(50):  # 最多等待 ~5s
                if self._initialized or self._terminating:
                    break
                await asyncio.sleep(0.1)

    async def terminate(self) -> None:
        self._terminating = True
        if self.event_handler:
            await self.event_handler.stop()
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        await self.store.close()
        logger.info("[Memoir] 已终止")

    # ==================== 钩子 ====================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._initialized or self.event_handler is None:
            return
        try:
            await self.event_handler.on_llm_request(event, req)
        except Exception as exc:
            logger.warning(f"[Memoir] 召回失败（不影响主流程）: {exc}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        if not self._initialized or self.event_handler is None:
            return
        try:
            await self.event_handler.on_llm_response(event, resp)
        except Exception as exc:
            logger.warning(f"[Memoir] 私聊编码失败（不影响主流程）: {exc}")

    @filter.custom_filter(PassiveGroupCaptureFilter, False)
    async def on_group_passive(self, event: AstrMessageEvent) -> None:
        """被动捕获所有群消息用于编码；filter 始终返回 False，不唤醒机器人。"""
        if not self._initialized or self.event_handler is None:
            return
        try:
            await self.event_handler.on_group_message(event)
        except Exception as exc:
            logger.warning(f"[Memoir] 群消息编码失败（不影响主流程）: {exc}")

    async def _dispatch_group_capture(self, event: AstrMessageEvent) -> None:
        """由 PassiveGroupCaptureFilter 触发的编码分发（内部任务调用入口）。"""
        if not self._initialized or self.event_handler is None:
            return
        try:
            await self.event_handler.on_group_message(event)
        except Exception as exc:
            logger.warning(f"[Memoir] 群消息编码失败（不影响主流程）: {exc}")
