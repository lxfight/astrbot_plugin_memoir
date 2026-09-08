"""
astrbot_plugin_memoir 主入口。

模拟人脑记忆闭环：捕获 -> 巩固 -> 遗忘 -> 召回 -> 洞察。
私聊与群聊采用不同策略，不使用向量检索/RAG。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config import AstrBotConfig

from .core.event_handler import EventHandler
from .core.passive_group_capture import (
    PassiveGroupCaptureFilter,
    clear_active_plugin,
    set_active_plugin,
)
from .core.scope import resolve_scope
from .core.storage import MemoryStore
from .core.web_api import WebApi

_PLUGIN_NAME = "astrbot_plugin_memoir"


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
        self._register_web_apis()

        # 非阻塞初始化：Provider 可能尚未就绪，异步启动避免阻塞 AstrBot 启动
        self._track_task(asyncio.create_task(self._initialize_async()))

    def _register_web_apis(self) -> None:
        """注册 WebUI 后端接口（pages/memoir/ 前端页面通过 bridge 调用）。

        核心无注销机制，handler 经弱引用访问插件实例，禁用/卸载后返回明确错误。
        """
        api = WebApi(self)
        prefix = f"/{_PLUGIN_NAME}"
        for path, handler, method, description in (
            ("browse", api.browse, "GET", "Browse filtered records"),
            ("raw/event", api.raw_event, "GET", "Read complete source event"),
            ("memories/update", api.edit_memory, "POST", "Edit memory text"),
            ("records/delete", api.delete_records, "POST", "Delete selected records"),
        ):
            self.context.register_web_api(
                f"{prefix}/{path}", handler, [method], description
            )

        self.context.register_web_api(
            f"{prefix}/overview", api.overview, ["GET"], "Memory overview"
        )
        self.context.register_web_api(
            f"{prefix}/processing", api.processing_status, ["GET"], "Processing status"
        )
        self.context.register_web_api(
            f"{prefix}/processing/retry",
            api.retry_processing,
            ["POST"],
            "Retry failed processing",
        )
        self.context.register_web_api(
            f"{prefix}/memories/sources",
            api.memory_sources,
            ["GET"],
            "Memory source turns",
        )
        self.context.register_web_api(
            f"{prefix}/memories", api.memories, ["GET"], "List/search memories"
        )
        self.context.register_web_api(
            f"{prefix}/memories/delete", api.delete_memory, ["POST"], "Delete a memory"
        )
        self.context.register_web_api(
            f"{prefix}/raw", api.raw_turns, ["GET"], "List raw turns"
        )
        self.context.register_web_api(
            f"{prefix}/raw/delete", api.delete_raw_turn, ["POST"], "Delete a raw turn"
        )
        self.context.register_web_api(
            f"{prefix}/scope/clear", api.clear_scope, ["POST"], "Clear a scope"
        )
        self.context.register_web_api(
            f"{prefix}/consents", api.consents, ["GET"], "List bridge consents"
        )
        self.context.register_web_api(
            f"{prefix}/consents/toggle",
            api.toggle_consent,
            ["POST"],
            "Toggle bridge consent",
        )
        self.context.register_web_api(
            f"{prefix}/config", api.get_global_config, ["GET"], "Get global config"
        )
        self.context.register_web_api(
            f"{prefix}/config/update",
            api.update_global_config,
            ["POST"],
            "Update global config",
        )
        self.context.register_web_api(
            f"{prefix}/scope-config",
            api.get_scope_config,
            ["GET"],
            "Get scope config override",
        )
        self.context.register_web_api(
            f"{prefix}/scope-config/update",
            api.update_scope_config,
            ["POST"],
            "Update scope config override",
        )

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
        # 先置为不可用，防止窗口期内事件/HTTP 请求触达已关闭的资源
        self._terminating = True
        self._initialized = False
        clear_active_plugin(self)
        try:
            if self.event_handler:
                await self.event_handler.stop()
            if self._background_tasks:
                for task in list(self._background_tasks):
                    task.cancel()
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
                self._background_tasks.clear()
        finally:
            await self.store.close()
            logger.info("[Memoir] 已终止")

    # ==================== 钩子 ====================

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
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
            logger.warning(f"[Memoir] 私聊捕获失败（不影响主流程）: {exc}")

    @filter.custom_filter(PassiveGroupCaptureFilter, False)
    async def on_group_passive(self, event: AstrMessageEvent) -> None:
        """Filter carrier only; this body never runs.

        PassiveGroupCaptureFilter performs the capture as a side effect
        during the waking-check stage and always returns False, so this
        handler is never activated. It exists solely so the filter gets
        registered with the event bus.
        """

    def submit_group_capture(self, event: AstrMessageEvent) -> None:
        """被动捕获的群消息：创建被跟踪的落库任务，terminate 时统一取消"""
        if not self._initialized or self._terminating:
            return
        try:
            scope = resolve_scope(event)
            generation = self.store.generations.get(
                (scope.scope_type, scope.scope_key), 0
            )
            self._track_task(
                asyncio.create_task(self._dispatch_group_capture(event, generation))
            )
        except RuntimeError:
            # 无运行中的事件循环（极端情况），忽略本次捕获
            pass

    async def _dispatch_group_capture(
        self, event: AstrMessageEvent, generation: int = 0
    ) -> None:
        """Dispatch capture queued by the passive group filter.

        Args:
            event: Incoming group message event.
            generation: Scope generation when capture was queued.
        """
        if not self._initialized or self.event_handler is None:
            return
        try:
            await self.event_handler.on_group_message(event, generation=generation)
        except Exception as exc:
            logger.warning(f"[Memoir] 群消息捕获失败（不影响主流程）: {exc}")

    # ==================== 指令 ====================

    _MEMORY_TYPE_NAMES = {"semantic": "认知", "insight": "洞察", "raw": "对话"}

    @filter.command("memoir")
    async def memoir(self, event: AstrMessageEvent, action: str = "", value: str = ""):
        """查看当前会话的记忆统计；consent 子指令管理群聊自我陈述桥接授权"""
        if not self._initialized:
            yield event.plain_result("[Memoir] 记忆系统尚未初始化完成，请稍后再试")
            return
        if action == "consent":
            async for result in self._handle_bridge_consent(event, value):
                yield result
            return
        scope = resolve_scope(event)
        stats = await self.store.get_scope_stats(scope.scope_type, scope.scope_key)
        if not stats:
            yield event.plain_result("当前会话还没有任何记忆")
            return
        parts = [
            f"{self._MEMORY_TYPE_NAMES.get(k, k)} {v} 条"
            for k, v in sorted(stats.items())
        ]
        yield event.plain_result(
            "当前会话记忆统计："
            + "，".join(parts)
            + "\n查看和管理记忆请打开 WebUI 的插件页面。"
        )

    async def _handle_bridge_consent(self, event: AstrMessageEvent, value: str):
        """群聊自我陈述桥接的个人授权（用户自助，无需管理员代操作）。

        授权记录按 (platform, sender_id) 保存；实际桥接仍受全局/会话桥接
        开关与敏感度上限约束。value 为空时展示当前状态与用法。
        """
        platform = event.get_platform_name()
        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("[Memoir] 无法识别发送者身份，授权失败")
            return
        flag = str(value).strip().lower()
        if flag in ("on", "enable", "1", "开", "开启"):
            await self.store.set_bridge_enabled(platform, sender_id, True)
            yield event.plain_result(
                "[Memoir] 已开启桥接授权：你在群聊中讲述自己的内容，巩固后可能"
                "进入你与机器人的私聊记忆（受敏感度过滤）。可用 /memoir consent off 关闭。"
            )
        elif flag in ("off", "disable", "0", "关", "关闭"):
            await self.store.set_bridge_enabled(platform, sender_id, False)
            yield event.plain_result("[Memoir] 已关闭桥接授权")
        else:
            enabled = await self.store.is_bridge_enabled(platform, sender_id)
            yield event.plain_result(
                f"[Memoir] 当前桥接授权：{'已开启' if enabled else '未开启'}。\n"
                "开启：/memoir consent on；关闭：/memoir consent off。"
            )
