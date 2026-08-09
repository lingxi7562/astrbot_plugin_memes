"""Delegated emotion-agent bridge for low-load meme decisions.

The normal conversation model only sees ``request_meme_review``.  The tool
starts a separate AstrBot Agent with a configured chat provider and gives that
agent the private ``send_meme`` tool.  The private agent owns emotion analysis;
the existing meme tool remains responsible for matching, policy, selection and
delivery.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass as std_dataclass
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .llm_schema import request_meme_review_parameters

_MAX_CONTEXT_MESSAGES = 12


@std_dataclass(frozen=True, slots=True)
class EmotionAgentSettings:
    """Bounded settings for the private emotion Agent."""

    provider_id: str = ""
    max_steps: int = 2
    timeout_seconds: float = 10.0

    @classmethod
    def safe(
        cls,
        *,
        provider_id: Any = "",
        max_steps: Any = 2,
        timeout_seconds: Any = 10.0,
    ) -> "EmotionAgentSettings":
        provider = provider_id.strip() if isinstance(provider_id, str) else ""
        try:
            steps = 2 if isinstance(max_steps, bool) else int(max_steps)
        except (TypeError, ValueError):
            steps = 2
        steps = max(1, min(4, steps))
        try:
            timeout = 10.0 if isinstance(timeout_seconds, bool) else float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = 10.0
        if not math.isfinite(timeout):
            timeout = 10.0
        timeout = max(1.0, min(60.0, timeout))
        return cls(provider_id=provider[:256], max_steps=steps, timeout_seconds=timeout)


@std_dataclass(frozen=True, slots=True)
class EmotionAgentRuntime:
    settings: EmotionAgentSettings
    meme_tool: Any


@dataclass
class _SingleUseMemeTool(FunctionTool[AstrAgentContext]):
    """Expose the real meme tool while making duplicate sends fail closed."""

    inner: Any = Field(default=None, exclude=True, repr=False)
    used: bool = Field(default=False, exclude=True, repr=False)

    @classmethod
    def from_tool(cls, tool: Any) -> "_SingleUseMemeTool":
        return cls(
            name=getattr(tool, "name", "send_meme"),
            description=getattr(tool, "description", ""),
            parameters=getattr(tool, "parameters", {}),
            inner=tool,
        )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if self.used:
            return "status=skipped\n本次情绪审核已经调用过 send_meme，不再重复发送。"
        self.used = True
        if self.inner is None:
            return "status=unavailable\nsend_meme 未初始化。"
        return await self.inner.call(context, **kwargs)


@dataclass
class EmotionDelegationTool(FunctionTool[AstrAgentContext]):
    """Signal a meme request and delegate the decision to an emotion Agent."""

    name: str = "request_meme_review"
    description: str = (
        "当你认为当前回复可能需要一张表情包时调用一次。"
        "无需自行分析主情绪，插件会让专用情绪模型阅读当前会话并决定是否发送。"
        "成功返回后不要再次调用，也不要直接调用 send_meme。"
    )
    parameters: dict = Field(default_factory=request_meme_review_parameters)
    # Keep arbitrary runtime objects out of the pydantic-generated tool schema.
    runtime: Any = Field(default=None, exclude=True, repr=False)

    @classmethod
    def create(
        cls,
        meme_tool: Any,
        *,
        provider_id: Any = "",
        max_steps: Any = 2,
        timeout_seconds: Any = 10.0,
    ) -> "EmotionDelegationTool":
        return cls(
            runtime=EmotionAgentRuntime(
                settings=EmotionAgentSettings.safe(
                    provider_id=provider_id,
                    max_steps=max_steps,
                    timeout_seconds=timeout_seconds,
                ),
                meme_tool=meme_tool,
            )
        )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        runtime = self.runtime
        if runtime is None or runtime.meme_tool is None:
            return (
                "status=unavailable\n"
                "情绪审核 Agent 未初始化，无需再次调用 request_meme_review。"
            )

        settings = runtime.settings
        if not settings.provider_id:
            logger.warning(
                "[astrbot_plugin_memes] delegated 模式未配置 emotion_provider_id"
            )
            return (
                "status=skipped\n"
                "未配置情绪 Agent Provider，无需再次调用 request_meme_review。"
            )

        event = context.context.event
        astrbot_context = context.context.context
        conversation_messages = getattr(context, "messages", [])
        if not isinstance(conversation_messages, list):
            conversation_messages = []
        conversation_messages = conversation_messages[-_MAX_CONTEXT_MESSAGES:]
        hint = kwargs.get("hint", "")
        if not isinstance(hint, str):
            hint = ""
        hint = " ".join(hint.replace("\x00", " ").split())[:256]
        prompt = (
            "分析当前会话最近的对话，判断是否真的需要发送表情包。"
            "如果需要，只调用一次 send_meme，并在 intent 中写一句简短的主情绪或回应；"
            "如果不需要，不要调用任何工具。"
        )
        if hint:
            prompt += f"\n对话模型提供的可选提示：{hint}"

        try:
            from astrbot.core.agent.tool import ToolSet

            private_meme_tool = _SingleUseMemeTool.from_tool(runtime.meme_tool)
            await asyncio.wait_for(
                astrbot_context.tool_loop_agent(
                    event=event,
                    chat_provider_id=settings.provider_id,
                    prompt=prompt,
                    contexts=conversation_messages or None,
                    system_prompt=(
                        "你是独立的情绪审核 Agent。你负责理解当前对话中的主要情绪，"
                        "仅在有明确表达价值时调用 send_meme。"
                        "不要解释推理，不要调用其它工具，不要重复调用。"
                    ),
                    tools=ToolSet([private_meme_tool]),
                    max_steps=settings.max_steps,
                    tool_call_timeout=settings.timeout_seconds,
                ),
                timeout=settings.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[astrbot_plugin_memes] 情绪 Agent 超时（{settings.timeout_seconds:.1f}s）"
            )
            return (
                "status=timeout\n"
                "情绪审核超时，未继续发送；无需再次调用 request_meme_review。"
            )
        except Exception as exc:
            logger.warning(f"[astrbot_plugin_memes] 情绪 Agent 调用失败: {exc}")
            return (
                "status=error\n"
                "情绪审核失败，未继续发送；无需再次调用 request_meme_review。"
            )

        return (
            "status=delegated\n"
            "情绪 Agent 已完成审核；如需发送已由 send_meme 处理，"
            "无需再次调用 request_meme_review。"
        )


__all__ = ["EmotionAgentRuntime", "EmotionAgentSettings", "EmotionDelegationTool"]
