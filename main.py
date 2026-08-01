from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .backend.embedder import MemeEmbedder
from .backend.index import MemeIndex, SourceConfigurationError
from .backend.matcher import TagMatcher
from .backend.thumbnails import ThumbnailManager, render_pillow_thumbnail
from .backend.tool import SendMemeTool
from .web_validation import (
    ValidationError,
    parse_library_sources,
    parse_list_query,
    safe_thumbnail_size,
    validate_config_payload,
)

PLUGIN_NAME = "astrbot_plugin_memes"

_THUMB_CACHE_LIMIT = 512
_THUMB_MAX_CONCURRENCY = 4
_THUMB_MAX_PENDING = 64


class MemesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._thumbnails = ThumbnailManager(
            render_pillow_thumbnail,
            cache_capacity=_THUMB_CACHE_LIMIT,
            max_concurrency=_THUMB_MAX_CONCURRENCY,
            max_pending=_THUMB_MAX_PENDING,
        )

        managed_root = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / PLUGIN_NAME
            / "library"
        )
        managed_root.mkdir(parents=True, exist_ok=True)
        sources: list[dict] = [
            {
                "type": "directory",
                "root": str(managed_root),
                "namespace": "managed",
                "recursive": True,
                "tags": [],
            }
        ]
        allowed_roots: list[Path] = [managed_root]

        try:
            configured_sources = parse_library_sources(
                config.get("library_sources", [])
            )
        except ValidationError as exc:
            configured_sources = []
            logger.warning(
                f"[{PLUGIN_NAME}] library_sources 配置无效，已安全忽略: {exc}"
            )
        for source in configured_sources:
            if not source["enabled"]:
                continue
            if source["__template_key"] == "json":
                sources.append(
                    {
                        "type": "json",
                        "index_path": source["index_path"],
                        "data_root": source["data_root"],
                        "namespace": source["namespace"],
                    }
                )
                allowed_roots.append(Path(source["data_root"]))
            else:
                sources.append(
                    {
                        "type": "directory",
                        "root": source["root"],
                        "namespace": source["namespace"],
                        "recursive": source["recursive"],
                        "tags": source["tags"],
                    }
                )
                allowed_roots.append(Path(source["root"]))

        legacy_index = ""
        legacy_root = ""
        legacy_index_value = config.get("index_path", "")
        legacy_root_value = config.get("data_root", "")
        if isinstance(legacy_index_value, str) and legacy_index_value.strip():
            try:
                candidate = Path(legacy_index_value.strip())
                if candidate.is_file():
                    legacy_index = str(candidate)
                    legacy_root_path = (
                        Path(legacy_root_value.strip())
                        if isinstance(legacy_root_value, str)
                        and legacy_root_value.strip()
                        else candidate.parent
                    )
                    legacy_root = str(legacy_root_path)
                    allowed_roots.append(legacy_root_path)
                else:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 旧版 index_path 不可用，已跳过；"
                        "独立模板库仍可加载"
                    )
            except (OSError, ValueError):
                logger.warning(
                    f"[{PLUGIN_NAME}] 旧版 index_path 配置无效，已安全忽略；"
                    "独立模板库仍可加载"
                )

        try:
            self.index = MemeIndex(
                legacy_index,
                legacy_root,
                sources=sources,
                allowed_roots=allowed_roots,
            )
        except SourceConfigurationError as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] 自定义模板来源配置无效，已仅启用管理目录: {exc}"
            )
            self.index = MemeIndex(
                sources=sources[:1],
                allowed_roots=allowed_roots[:1],
            )

        if config.get("auto_refresh", True):
            try:
                self._load_index()
                logger.info(
                    f"[{PLUGIN_NAME}] 已从索引加载 {self.index.count} 张表情包"
                )
            except Exception:
                logger.error(
                    f"[{PLUGIN_NAME}] 加载模板库失败，请通过 /sources 查看来源状态"
                )

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

        max_candidates = config.get("max_match_candidates", 10)
        min_score = config.get("min_tag_score", 0.0)
        SendMemeTool.configure(
            self.matcher,
            self.index,
            max_candidates,
            min_score,
            match_mode=match_mode,
            embedding_fallback=config.get("embedding_fallback", True),
        )
        self.context.add_llm_tools(SendMemeTool())

        self._register_web_apis()

    def _load_index(self) -> dict:
        """Load atomically, then invalidate thumbnails only after success."""

        return self._thumbnails.run_index_load(self.index.load)

    def _index_is_loaded(self) -> bool:
        return bool(self.index.get_status_report().get("committed"))

    def _register_web_apis(self) -> None:
        ctx = self.context
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/list",
            self._api_list,
            ["GET"],
            "分页获取表情包列表",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/thumbnail",
            self._api_thumbnail,
            ["GET"],
            "按需获取表情包缩略图",
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
            f"/{PLUGIN_NAME}/sources",
            self._api_sources,
            ["GET"],
            "获取模板来源健康报告",
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
        try:
            query = parse_list_query(
                {
                    "page": request.query.get("page"),
                    "page_size": request.query.get("page_size"),
                    "q": request.query.get("q"),
                    "tag": request.query.get("tag"),
                    "sort": request.query.get("sort", "filename"),
                }
            )
        except ValidationError as exc:
            return error_response(str(exc), status_code=400)

        try:
            if not self._index_is_loaded():
                self._load_index()
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] WebUI 加载索引失败: {exc}")
            return error_response("无法加载表情包索引", status_code=500)

        items: list[dict] = []
        for img_id, item in self.index.images.items():
            if not isinstance(img_id, str) or not isinstance(item, dict):
                continue
            filename = item.get("filename", "")
            if not isinstance(filename, str):
                filename = ""
            raw_tags = item.get("tags", [])
            tags = (
                [tag for tag in raw_tags if isinstance(tag, str)]
                if isinstance(raw_tags, list)
                else []
            )
            items.append(
                {
                    "id": img_id,
                    "filename": filename,
                    "tags": tags,
                    "source": item.get("source", "")
                    if isinstance(item.get("source"), str)
                    else "",
                    # Keep the legacy key while moving thumbnail generation out of
                    # the list request. The updated Page loads it only when visible.
                    "thumb_b64": "",
                    "thumbnail_endpoint": "thumbnail",
                }
            )

        query_text = query.q.casefold()
        tag_text = query.tag.casefold()
        if query_text:
            items = [
                item
                for item in items
                if query_text in item["filename"].casefold()
                or any(query_text in tag.casefold() for tag in item["tags"])
            ]
        if tag_text:
            items = [
                item
                for item in items
                if any(tag_text == tag.casefold() for tag in item["tags"])
            ]

        reverse = query.sort.endswith("_desc")
        if query.sort.startswith("id"):
            sort_key = lambda item: item["id"].casefold()
        elif query.sort.startswith("tag_count"):
            sort_key = lambda item: len(item["tags"])
        else:
            sort_key = lambda item: (item["filename"].casefold(), item["id"].casefold())
        items.sort(key=sort_key, reverse=reverse)

        total = len(items)
        start = (query.page - 1) * query.page_size
        page_items = items[start : start + query.page_size]
        pages = (total + query.page_size - 1) // query.page_size
        return json_response(
            {
                "items": page_items,
                "page": query.page,
                "page_size": query.page_size,
                "total": total,
                "pages": pages,
                "has_next": query.page < pages,
            }
        )

    async def _api_thumbnail(self):
        img_id = request.query.get("id")
        if not isinstance(img_id, str) or not img_id or len(img_id) > 512:
            return error_response("id 无效", status_code=400)

        item = self.index.images.get(img_id)
        if not isinstance(item, dict):
            return error_response("表情包不存在", status_code=404)

        size = safe_thumbnail_size(self.config.get("thumbnail_size", 200))
        return json_response(
            {
                "id": img_id,
                "thumb_b64": await self._get_thumbnail_b64(img_id, item, size),
            }
        )

    async def _api_refresh(self):
        try:
            report = self._load_index()
            return json_response(
                {
                    "status": "ok",
                    "count": self.index.count,
                    "tags": len(self.index.get_unique_tags()),
                    "summary": {
                        key: report.get(key)
                        for key in (
                            "status",
                            "committed",
                            "count",
                            "source_count",
                            "missing_file_count",
                            "duplicate_count",
                            "error_count",
                        )
                    },
                }
            )
        except Exception:
            logger.error(
                f"[{PLUGIN_NAME}] WebUI 刷新模板库失败，请通过 /sources 查看来源状态"
            )
            return error_response("刷新表情包索引失败", status_code=500)

    async def _api_tags(self):
        try:
            if not self._index_is_loaded():
                self._load_index()
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] WebUI 加载标签失败: {exc}")
            return error_response("无法加载标签", status_code=500)
        return json_response(self.index.get_unique_tags())

    async def _api_sources(self):
        return json_response(self.index.get_status_report())

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
            "library_sources": self.config.get("library_sources", []),
            "embedder_status": "on" if self.embedder is not None else "off",
            "available_embedding_providers": emb_providers,
        })

    async def _api_set_config(self):
        body = await request.json(default={})
        try:
            provider_ids = {
                meta.id
                for provider in self.context.get_all_embedding_providers()
                if (meta := provider.meta()) and isinstance(meta.id, str)
            }
            validated = validate_config_payload(body, provider_ids)
        except ValidationError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 获取 Embedding Provider 失败: {exc}")
            return error_response("无法验证 Embedding Provider", status_code=500)

        changed = {
            key: value
            for key, value in validated.items()
            if self.config.get(key) != value
        }
        if not changed:
            return json_response({"status": "ok", "message": "无变更"})

        previous = {key: self.config.get(key) for key in changed}
        try:
            for key, value in changed.items():
                self.config[key] = value
            self.config.save_config()
        except Exception as exc:
            for key, value in previous.items():
                self.config[key] = value
            logger.error(f"[{PLUGIN_NAME}] 保存 Page 配置失败: {exc}")
            return error_response("保存配置失败", status_code=500)
        return json_response({"status": "ok", "message": "配置已保存，重载插件后生效"})

    async def _get_thumbnail_b64(self, img_id: str, item: dict, size: int) -> str:
        try:
            img_path = self.index.get_abs_path(item)
        except Exception:
            return ""
        return await self._thumbnails.get_thumbnail(img_id, img_path, size)

    async def terminate(self) -> None:
        await self._thumbnails.close()
