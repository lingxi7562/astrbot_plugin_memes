from pathlib import Path

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext


@dataclass
class SendMemeTool(FunctionTool[AstrAgentContext]):
    name: str = "send_meme"
    description: str = (
        "从表情包库中挑选并发送一张表情包图片到当前对话。"
        "当回复适合配表情包时调用——比如表达开心、无语、生气等情绪，"
        "或者回怼、自嘲、吐槽等场景。"
        "tags 里填入你想传达的情绪和内容关键词，尽量用常见中文词。"
        "例如对方说了个冷笑话，你可以传 tags=[\"无语\", \"冷笑\"]；"
        "对方说想你了，传 tags=[\"害羞\", \"想你\", \"可爱\"]。"
        "scene 是可选的补充说明，帮你描述当前对话氛围。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "表情包标签，描述你想传递的情绪或内容。"
                        "例如 [\"开心\", \"祝贺\", \"比心\"]；"
                        "例如 [\"生气\", \"怼人\", \"你这瓜保熟吗\"]。"
                        "尽量用简短的中文词，1-4 个标签即可。"
                    ),
                },
                "scene": {
                    "type": "string",
                    "description": (
                        "可选，对话场景或情绪的补充描述。"
                        "例如 \"对方在撒娇\"、\"刚才讲了个冷笑话\"、\"收到表扬很开心\"。"
                        "用于辅助更精准地匹配表情包。"
                    ),
                },
            },
            "required": ["tags"],
        }
    )

    _matcher = None
    _index = None
    _max_candidates: int = 10
    _min_score: float = 0.0
    _match_mode: str = "keyword"
    _embedding_fallback: bool = True

    @classmethod
    def configure(
        cls,
        matcher,
        index,
        max_candidates: int = 10,
        min_score: float = 0.0,
        match_mode: str = "keyword",
        embedding_fallback: bool = True,
    ):
        cls._matcher = matcher
        cls._index = index
        cls._max_candidates = max_candidates
        cls._min_score = min_score
        cls._match_mode = match_mode
        cls._embedding_fallback = embedding_fallback

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        tags: list[str] = kwargs.get("tags", [])
        scene: str = kwargs.get("scene", "")
        if scene:
            tags.append(scene)
        if not tags:
            return ToolExecResult(
                content=[{"type": "text", "text": "请提供至少一个表情包标签。"}]
            )

        matcher = SendMemeTool._matcher
        index = SendMemeTool._index
        if matcher is None or index is None:
            return ToolExecResult(
                content=[
                    {"type": "text", "text": "表情包匹配器未初始化，请联系管理员。"}
                ]
            )

        match_mode = SendMemeTool._match_mode
        limit = SendMemeTool._max_candidates
        min_score = SendMemeTool._min_score
        do_fallback = SendMemeTool._embedding_fallback

        matches: list = []
        if match_mode == "embedding":
            matches = await matcher.match_embedding(tags, limit=limit)
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
            return ToolExecResult(
                content=[
                    {
                        "type": "text",
                        "text": f"未找到匹配的表情包。传入标签: {', '.join(tags)}，"
                        f"库中部分可用标签: {', '.join(available)}",
                    }
                ]
            )

        best = matches[0]
        img_path = Path(best["path"])
        if not img_path.is_file():
            for candidate in matches[1:]:
                alt = Path(candidate["path"])
                if alt.is_file():
                    best = candidate
                    img_path = alt
                    break
            else:
                return ToolExecResult(
                    content=[
                        {
                            "type": "text",
                            "text": f"表情包文件不存在: {best['filename']}",
                        }
                    ]
                )

        try:
            event = context.context.event
            await event.send(event.chain_result([Image.fromFileSystem(str(img_path))]))
            logger.info(
                f"[astrbot_plugin_memes] 发送表情包: {best['filename']} "
                f"(标签: {', '.join(best['matched_tags'])})"
            )
        except Exception as e:
            logger.error(f"[astrbot_plugin_memes] 发送表情包失败: {e}")
            return ToolExecResult(
                content=[{"type": "text", "text": f"发送表情包时出错: {e}"}]
            )

        return ToolExecResult(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"已发送表情包。文件名: {best['filename']}，"
                        f"匹配标签: {', '.join(best['matched_tags'])}"
                    ),
                }
            ]
        )
