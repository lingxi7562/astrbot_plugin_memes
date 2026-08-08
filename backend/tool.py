from pathlib import Path
from dataclasses import dataclass as std_dataclass
from typing import Any, ClassVar

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .analytics import MemeAnalytics
from .policy import MemePolicy, PolicySettings
from .query import build_query_plan
from .routing import MemeRouter, RoutingSettings
from .sender import MemeSender, SendPipelineSettings
from .selector import MemeSelector, SelectionSettings
from .llm_schema import send_meme_parameters


def _tool_result(*, content: list[dict[str, Any]]) -> ToolExecResult:
    """Build AstrBot's MCP tool result without assuming a union alias is callable.

    AstrBot exposes ``ToolExecResult`` as ``str | CallToolResult``.  Older plugin
    code sometimes called that type alias directly, which fails at runtime on
    current releases.  Keep the import lazy for maintenance tooling and fall
    back to plain text for runtimes that do not ship MCP types.
    """

    try:
        from mcp.types import CallToolResult, TextContent

        blocks = [
            TextContent(type="text", text=item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        ]
        return CallToolResult(content=blocks)
    except Exception:
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )


@std_dataclass(frozen=True, slots=True)
class SendMemeRuntime:
    matcher: Any
    index: Any
    max_candidates: int
    min_score: float
    match_mode: str
    embedding_fallback: bool
    selector: MemeSelector | None
    selection_settings: SelectionSettings
    analytics: MemeAnalytics | None
    router: MemeRouter | None
    policy: MemePolicy | None
    sender: MemeSender


@dataclass
class SendMemeTool(FunctionTool[AstrAgentContext]):
    name: str = "send_meme"
    description: str = (
        "发送一张最合适的表情包。通常只需调用一次并填写 intent："
        "用一句短话描述想表达的感觉或回应，例如‘对方讲冷笑话，我想无语吐槽’。"
        "插件会自动完成匹配、路由、去重、策略检查和发送；成功后不要再次调用。"
        "tags、scene、pack、persona 仅为兼容或高级选项，通常留空。"
    )
    parameters: dict = Field(default_factory=send_meme_parameters)

    _matcher: ClassVar[Any] = None
    _index: ClassVar[Any] = None
    _max_candidates: ClassVar[int] = 10
    _min_score: ClassVar[float] = 0.0
    _match_mode: ClassVar[str] = "keyword"
    _embedding_fallback: ClassVar[bool] = True
    _selector: ClassVar[MemeSelector | None] = None
    _selection_settings: ClassVar[SelectionSettings] = SelectionSettings()
    _analytics: ClassVar[MemeAnalytics | None] = None
    _router: ClassVar[MemeRouter | None] = None
    _routing_settings: ClassVar[RoutingSettings] = RoutingSettings()
    _policy: ClassVar[MemePolicy | None] = None
    _policy_settings: ClassVar[PolicySettings] = PolicySettings()
    _sender: ClassVar[MemeSender | None] = None
    _pipeline_settings: ClassVar[SendPipelineSettings] = SendPipelineSettings()
    # AstrBot's pydantic dataclass must not generate a schema for runtime-only
    # objects (MemeSelector, MemeAnalytics, and platform adapters are arbitrary
    # types).  Keep the field untyped at the pydantic boundary and validate it
    # through the frozen runtime dataclass created below.
    runtime: Any = Field(default=None, exclude=True, repr=False)

    @classmethod
    def configure(
        cls,
        matcher,
        index,
        max_candidates: int = 10,
        min_score: float = 0.0,
        match_mode: str = "keyword",
        embedding_fallback: bool = True,
        selector: MemeSelector | None = None,
        selection_settings: SelectionSettings | None = None,
        analytics: MemeAnalytics | None = None,
        router: MemeRouter | None = None,
        routing_settings: RoutingSettings | None = None,
        policy: MemePolicy | None = None,
        policy_settings: PolicySettings | None = None,
        sender: MemeSender | None = None,
        pipeline_settings: SendPipelineSettings | None = None,
    ):
        cls._matcher = matcher
        cls._index = index
        cls._max_candidates = max_candidates
        cls._min_score = min_score
        cls._match_mode = match_mode
        cls._embedding_fallback = embedding_fallback
        cls._selector = selector
        cls._selection_settings = selection_settings or SelectionSettings()
        cls._analytics = analytics
        cls._router = router
        cls._routing_settings = routing_settings or RoutingSettings()
        cls._policy = policy
        cls._policy_settings = policy_settings or PolicySettings()
        cls._sender = sender
        cls._pipeline_settings = pipeline_settings or SendPipelineSettings()

    @classmethod
    def create(
        cls,
        matcher,
        index,
        max_candidates: int = 10,
        min_score: float = 0.0,
        match_mode: str = "keyword",
        embedding_fallback: bool = True,
        selector: MemeSelector | None = None,
        selection_settings: SelectionSettings | None = None,
        analytics: MemeAnalytics | None = None,
        router: MemeRouter | None = None,
        policy: MemePolicy | None = None,
        sender: MemeSender | None = None,
    ) -> "SendMemeTool":
        runtime = SendMemeRuntime(
            matcher=matcher,
            index=index,
            max_candidates=max_candidates,
            min_score=min_score,
            match_mode=match_mode,
            embedding_fallback=embedding_fallback,
            selector=selector,
            selection_settings=selection_settings or SelectionSettings(),
            analytics=analytics,
            router=router,
            policy=policy,
            sender=sender or MemeSender(),
        )
        return cls(runtime=runtime)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        runtime = self.runtime
        matcher = runtime.matcher if runtime is not None else SendMemeTool._matcher
        index = runtime.index if runtime is not None else SendMemeTool._index
        if matcher is None or index is None:
            return _tool_result(
                content=[
                    {"type": "text", "text": "表情包匹配器未初始化，请联系管理员。"}
                ]
            )

        event = context.context.event
        try:
            known_tags = index.get_unique_tags()
        except Exception:
            known_tags = ()
        query_plan = build_query_plan(
            intent=kwargs.get("intent", ""),
            tags=kwargs.get("tags", ()),
            scene=kwargs.get("scene", ""),
            context=self._event_message(event),
            known_tags=known_tags,
        )
        tags = list(query_plan.terms)
        if query_plan.is_empty:
            return _tool_result(
                content=[
                    {
                        "type": "text",
                        "text": (
                            "未能确定想表达的情绪。下一次只需提供 intent，"
                            "例如‘开心地打招呼’。"
                        ),
                    }
                ]
            )

        match_mode = runtime.match_mode if runtime is not None else SendMemeTool._match_mode
        limit = runtime.max_candidates if runtime is not None else SendMemeTool._max_candidates
        min_score = runtime.min_score if runtime is not None else SendMemeTool._min_score
        do_fallback = (
            runtime.embedding_fallback
            if runtime is not None
            else SendMemeTool._embedding_fallback
        )

        matches: list = []
        if match_mode == "embedding":
            matches = await matcher.match_embedding(
                tags,
                limit=limit,
                min_score=min_score,
            )
            if not matches and do_fallback:
                matches = matcher.match(tags, limit=limit, min_score=min_score)
        elif match_mode == "hybrid":
            matches = await matcher.match_hybrid(tags, limit=limit, min_score=min_score)
            if not matches and do_fallback:
                matches = matcher.match(tags, limit=limit, min_score=min_score)
        else:
            matches = matcher.match(tags, limit=limit, min_score=min_score)

        if not matches:
            available = index.get_unique_tags()[:50]
            return _tool_result(
                content=[
                    {
                        "type": "text",
                        "text": f"未找到匹配的表情包。传入标签: {', '.join(tags)}，"
                        f"库中部分可用标签: {', '.join(available)}",
                    }
                ]
            )

        valid_matches = []
        for candidate in matches:
            path_value = candidate.get("path")
            if isinstance(path_value, str) and path_value and Path(path_value).is_file():
                valid_matches.append(candidate)
        if not valid_matches:
            return _tool_result(
                content=[
                    {
                        "type": "text",
                        "text": "匹配到的表情包文件已不存在，请先刷新模板库。",
                    }
                ]
            )

        scope = self._event_scope(event)
        pack = kwargs.get("pack", "")
        if not isinstance(pack, str):
            pack = ""
        persona = kwargs.get("persona", "")
        if not isinstance(persona, str):
            persona = ""
        router = runtime.router if runtime is not None else SendMemeTool._router
        route_info = {"pack": "", "fallback": False}
        if router is not None:
            valid_matches, route_info = router.route(
                valid_matches,
                scope=scope,
                pack=pack[:64],
                persona=persona[:64],
            )
        policy = runtime.policy if runtime is not None else SendMemeTool._policy
        policy_decision = None
        if policy is not None:
            policy_decision = policy.reserve(
                valid_matches,
                scope=scope,
                query_tags=tags,
            )
            if not policy_decision.allowed:
                return _tool_result(
                    content=[
                        {
                            "type": "text",
                            "text": f"当前发送策略暂不允许发送（{policy_decision.reason}）。",
                        }
                    ]
                )
            valid_matches = list(policy_decision.candidates)
        selector = runtime.selector if runtime is not None else SendMemeTool._selector
        analytics = runtime.analytics if runtime is not None else SendMemeTool._analytics
        if analytics is not None:
            try:
                valid_matches = analytics.personalize(valid_matches, scope=scope)
            except Exception as exc:
                logger.warning(f"[astrbot_plugin_memes] 个性化排序不可用，已使用原始候选: {exc}")
        if selector is not None:
            best = selector.choose(
                valid_matches,
                scope=scope,
                settings=(
                    runtime.selection_settings
                    if runtime is not None
                    else SendMemeTool._selection_settings
                ),
            )
        else:
            best = valid_matches[0]
        if best is None:
            if policy is not None and policy_decision is not None:
                policy.release(scope, policy_decision.reservation_id)
            return _tool_result(
                content=[{"type": "text", "text": "暂时没有可发送的表情包。"}]
            )
        img_path = Path(best["path"])

        try:
            sender = runtime.sender if runtime is not None else SendMemeTool._sender
            await (sender or MemeSender()).send(event, img_path)
            if analytics is not None:
                try:
                    analytics.record_send(scope, best.get("id"), best.get("tags", []))
                except Exception as exc:
                    logger.warning(f"[astrbot_plugin_memes] 发送分析记录失败: {exc}")
            logger.info(
                f"[astrbot_plugin_memes] 发送表情包: {best['filename']} "
                f"(标签: {', '.join(best['matched_tags'])}; "
                f"pack: {route_info.get('pack', '') or 'all'})"
            )
        except Exception as e:
            if selector is not None:
                selector.release(best, scope=scope)
            if policy is not None and policy_decision is not None:
                policy.release(scope, policy_decision.reservation_id)
            if analytics is not None:
                try:
                    analytics.record_failure(scope, best.get("id"))
                except Exception as exc:
                    logger.warning(f"[astrbot_plugin_memes] 发送失败分析记录失败: {exc}")
            logger.error(f"[astrbot_plugin_memes] 发送表情包失败: {e}")
            return _tool_result(
                content=[{"type": "text", "text": f"发送表情包时出错: {e}"}]
            )

        return _tool_result(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"status=sent\nimage={best['filename']}\n"
                        "无需再次调用 send_meme。"
                    ),
                }
            ]
        )

    @staticmethod
    def _event_message(event: object) -> str:
        try:
            value = getattr(event, "message_str", "")
            if callable(value):
                value = value()
            return value if isinstance(value, str) else ""
        except Exception:
            return ""

    @staticmethod
    def _event_scope(event: object) -> str:
        for attribute in ("unified_msg_origin", "session_id"):
            try:
                value = getattr(event, attribute, None)
                if callable(value):
                    value = value()
                if isinstance(value, str) and value.strip():
                    return value.strip()[:512]
            except Exception:
                continue
        return "global"
