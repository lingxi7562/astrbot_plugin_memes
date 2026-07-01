import base64
import io
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import json_response, request
from astrbot.core.agent.message import TextPart

from .backend.embedder import MemeEmbedder
from .backend.index import MemeIndex
from .backend.matcher import TagMatcher

PLUGIN_NAME = "astrbot_plugin_memes"

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

_THUMB_CACHE: dict[str, str] = {}


class MemesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        index_path = config.get("index_path", "")
        data_root = config.get("data_root", "")
        self.index = MemeIndex(index_path, data_root)

        if config.get("auto_refresh", True):
            try:
                self.index.load()
                logger.info(
                    f"[{PLUGIN_NAME}] 已从索引加载 {self.index.count} 张表情包"
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 加载索引失败: {e}")

        self.matcher = TagMatcher(
            self.index,
            config.get("tag_synonyms", {}) if isinstance(config.get("tag_synonyms"), dict) else {},
        )

        match_mode = config.get("match_mode", "keyword")
        self.embedder: MemeEmbedder | None = None

        if match_mode in ("embedding", "hybrid"):
            emb_providers = self.context.get_all_embedding_providers()
            emb_provider_id = config.get("embedding_provider_id", "")
            emb_provider = None
            if emb_provider_id:
                emb_provider = self.context.get_provider_by_id(emb_provider_id)
            if emb_provider is None and emb_providers:
                emb_provider = emb_providers[0]

            if emb_provider is not None:
                self.embedder = MemeEmbedder(self.index, emb_provider)
                self.matcher.set_embedder(self.embedder)
                logger.info(
                    f"[{PLUGIN_NAME}] 已绑定 Embedding Provider，匹配模式: {match_mode}"
                )
            else:
                logger.warning(
                    f"[{PLUGIN_NAME}] 未找到可用的 Embedding Provider，"
                    f"回退到 keyword 模式"
                )
                match_mode = "keyword"

        self._match_mode = match_mode
        self._max_candidates = config.get("max_match_candidates", 10)
        self._min_score = config.get("min_tag_score", 0.0)
        self._embedding_fallback = config.get("embedding_fallback", True)

        self._register_web_apis()

    @filter.llm_tool(name="send_meme")
    async def send_meme_tool(
        self, event: AstrMessageEvent, tags: list[str], scene: str = ""
    ) -> MessageEventResult:
        '''当需要表达情绪或活跃气氛时调用，从表情包库中挑选并发送一张表情包图片。

        Args:
            tags(array[string]): 表情包标签，描述想传递的情绪。例如 ["开心","祝贺","比心"]，["无语","冷笑"]
            scene(string): 可选场景描述，例如 "对方在撒娇"、"刚讲了个冷笑话"
        '''
        if scene:
            tags.append(scene)
        if not tags:
            yield event.plain_result("请提供至少一个表情包标签。")
            return

        limit = self._max_candidates
        min_score = self._min_score

        matches: list = []
        if self._match_mode == "embedding":
            matches = await self.matcher.match_embedding(tags, limit=limit)
            if not matches and self._embedding_fallback:
                matches = self.matcher.match(tags, limit=limit, min_score=min_score)
        elif self._match_mode == "hybrid":
            matches = await self.matcher.match_hybrid(tags, limit=limit, min_score=min_score)
            if not matches and self._embedding_fallback:
                matches = self.matcher.match(tags, limit=limit, min_score=min_score)
        else:
            matches = self.matcher.match(tags, limit=limit, min_score=min_score)

        if not matches:
            available = self.index.get_unique_tags()[:50]
            yield event.plain_result(
                f"未找到匹配的表情包。传入标签: {', '.join(tags)}，"
                f"库中部分可用标签: {', '.join(available)}"
            )
            return

        best = matches[0]
        img_path = Path(best["path"])
        if not img_path.is_file():
            found = False
            for candidate in matches[1:]:
                alt = Path(candidate["path"])
                if alt.is_file():
                    best = candidate
                    img_path = alt
                    found = True
                    break
            if not found:
                yield event.plain_result(f"表情包文件不存在: {best['filename']}")
                return

        try:
            yield event.chain_result([Image.fromFileSystem(str(img_path))])
            logger.info(
                f"[{PLUGIN_NAME}] 发送表情包: {best['filename']} "
                f"(标签: {', '.join(best['matched_tags'])})"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 发送表情包失败: {e}")
            yield event.plain_result(f"发送表情包时出错: {e}")

    @filter.on_llm_request()
    async def inject_meme_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("inject_prompt_enabled", False):
            return
        prompt = self.config.get("inject_prompt", "")
        if not prompt or not isinstance(prompt, str):
            return

        if "{tag_list}" in prompt:
            static_prompt = prompt.replace("{tag_list}", "")
            req.system_prompt += static_prompt
            tag_list = ", ".join(self.index.get_unique_tags()[:80])
            req.extra_user_content_parts.append(
                TextPart(text=f"\n<memes_available_tags>\n{tag_list}\n</memes_available_tags>")
            )
        else:
            req.system_prompt += prompt

    def _register_web_apis(self) -> None:
        ctx = self.context
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/list",
            self._api_list,
            ["GET"],
            "获取表情包列表（含缩略图 base64）",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/refresh",
            self._api_refresh,
            ["POST"],
            "刷新表情包索引",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/tags",
            self._api_tags,
            ["GET"],
            "获取所有标签列表",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/config",
            self._api_get_config,
            ["GET"],
            "获取当前插件配置",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/config",
            self._api_set_config,
            ["POST"],
            "更新插件配置并保存",
        )

    async def _api_list(self):
        page = int(request.query.get("page", 1))
        page_size = int(request.query.get("page_size", 30))
        search = (request.query.get("search", "") or "").strip()

        all_ids = sorted(self.index.images.keys())

        if search:
            search_lower = search.lower()
            all_ids = [
                iid
                for iid in all_ids
                if search_lower in (self.index.images[iid].get("filename", "") or "").lower()
                or any(
                    search_lower in t.lower()
                    for t in self.index.images[iid].get("tags", [])
                )
            ]

        total = len(all_ids)
        start = (page - 1) * page_size
        end = min(start + page_size, total)
        page_ids = all_ids[start:end]

        items = []
        thumb_size = self.config.get("thumbnail_size", 200)
        for img_id in page_ids:
            item = self.index.images[img_id]
            thumb_b64 = self._get_thumbnail_b64(item, thumb_size)
            items.append(
                {
                    "id": img_id,
                    "filename": item.get("filename", ""),
                    "tags": item.get("tags", []),
                    "thumb_b64": thumb_b64,
                }
            )

        return json_response(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_more": end < total,
            }
        )

    async def _api_refresh(self):
        try:
            self.index.load()
            global _THUMB_CACHE
            _THUMB_CACHE = {}
            return json_response(
                {
                    "status": "ok",
                    "count": self.index.count,
                    "tags": len(self.index.get_unique_tags()),
                }
            )
        except Exception as e:
            return json_response({"status": "error", "message": str(e)})

    async def _api_tags(self):
        try:
            self.index.load()
        except Exception:
            pass
        return json_response(self.index.get_unique_tags())

    async def _api_get_config(self):
        emb_providers = []
        for p in self.context.get_all_embedding_providers():
            meta = p.meta()
            emb_providers.append({"id": meta.id, "type": meta.type, "dim": p.get_dim()})
        return json_response({
            "match_mode": self.config.get("match_mode", "keyword"),
            "embedding_provider_id": self.config.get("embedding_provider_id", ""),
            "embedding_fallback": self.config.get("embedding_fallback", True),
            "max_match_candidates": self.config.get("max_match_candidates", 10),
            "min_tag_score": self.config.get("min_tag_score", 0.0),
            "auto_refresh": self.config.get("auto_refresh", True),
            "thumbnail_size": self.config.get("thumbnail_size", 200),
            "inject_prompt_enabled": self.config.get("inject_prompt_enabled", False),
            "inject_prompt": self.config.get("inject_prompt", ""),
            "embedder_status": "on" if self.embedder is not None else "off",
            "available_embedding_providers": emb_providers,
        })

    async def _api_set_config(self):
        try:
            body = await request.json()
        except Exception:
            return json_response({"status": "error", "message": "请求体解析失败"})

        allowed_keys = {
            "match_mode", "embedding_provider_id", "embedding_fallback",
            "max_match_candidates", "min_tag_score", "auto_refresh", "thumbnail_size",
            "inject_prompt_enabled", "inject_prompt",
        }
        changed = False
        for k, v in body.items():
            if k in allowed_keys:
                self.config[k] = v
                changed = True

        if changed:
            self.config.save_config()
            return json_response({"status": "ok", "message": "配置已保存，重载插件后生效"})
        return json_response({"status": "ok", "message": "无变更"})

    def _get_thumbnail_b64(self, item: dict, size: int) -> str:
        img_id = item.get("id", "")
        if img_id in _THUMB_CACHE:
            return _THUMB_CACHE[img_id]

        if PILImage is None:
            _THUMB_CACHE[img_id] = ""
            return ""

        img_path = self.index.get_abs_path(item)
        try:
            with PILImage.open(img_path) as img:
                img.thumbnail((size, size), PILImage.Resampling.LANCZOS)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=60)
                data = base64.b64encode(buf.getvalue()).decode("ascii")
                _THUMB_CACHE[img_id] = data
                return data
        except Exception:
            _THUMB_CACHE[img_id] = ""
            return ""

    async def terminate(self) -> None:
        global _THUMB_CACHE
        _THUMB_CACHE = {}
