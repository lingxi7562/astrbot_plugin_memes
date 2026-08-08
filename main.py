from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .backend.analytics import AnalyticsSettings, MemeAnalytics
from .backend.backup import BackupError, BackupManager
from .backend.catalog import CatalogError, ManagedCatalog
from .backend.embedder import MemeEmbedder
from .backend.index import MemeIndex, SourceConfigurationError
from .backend.matcher import TagMatcher
from .backend.policy import MemePolicy, PolicySettings
from .backend.routing import MemeRouter, RoutingSettings
from .backend.selector import MemeSelector, SelectionSettings
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
        self.selector = MemeSelector()
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
        self.managed_root = managed_root
        self.catalog = ManagedCatalog(
            managed_root,
            managed_root.parent / "managed_metadata.json",
        )
        self.backups = BackupManager(
            managed_root,
            managed_root.parent / "managed_metadata.json",
            managed_root.parent / "backups",
            retention_count=config.get("backup_retention_count", 20),
        )
        self.routing_settings = RoutingSettings.safe(
            packs=config.get("meme_packs", []),
            default_pack=config.get("default_pack", ""),
            persona_packs=config.get("persona_packs", {}),
            sticky_sessions=config.get("sticky_sessions", True),
        )
        self.router = MemeRouter(self.routing_settings)
        self.policy_settings = PolicySettings.safe(
            enabled=config.get("policy_enabled", True),
            quota_window_seconds=config.get("quota_window_seconds", 60.0),
            quota_max_sends=config.get("quota_max_sends", 8),
            blocked_tags=config.get("blocked_tags", []),
            allowed_tags=config.get("allowed_tags", []),
            blocked_namespaces=config.get("blocked_namespaces", []),
            blocked_ids=config.get("blocked_ids", []),
            max_file_bytes=config.get("max_file_bytes", 20 * 1024 * 1024),
        )
        self.policy = MemePolicy(self.policy_settings)
        self.analytics = MemeAnalytics(
            managed_root.parent / "analytics.json",
            AnalyticsSettings.safe(
                enabled=config.get("analytics_enabled", True),
                retention_days=config.get("analytics_retention_days", 30),
                personalization_strength=config.get(
                    "personalization_strength", 0.5
                ),
            ),
        )
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
        self.match_mode = match_mode
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
                self.embedder = MemeEmbedder(
                    self.index,
                    emb_provider,
                    cache_path=managed_root.parent / "embedding_cache.json",
                )
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
        self.match_mode = match_mode

        max_candidates = config.get("max_match_candidates", 10)
        min_score = config.get("min_tag_score", 0.0)
        self.selection_settings = SelectionSettings.safe(
            mode=config.get("selection_mode", "weighted"),
            pool_size=config.get("selection_pool_size", 5),
            cooldown_seconds=config.get("selection_cooldown_seconds", 300.0),
            history_size=config.get("selection_history_size", 20),
            deduplicate_files=config.get("deduplicate_files", True),
        )
        SendMemeTool.configure(
            self.matcher,
            self.index,
            max_candidates,
            min_score,
            match_mode=match_mode,
            embedding_fallback=config.get("embedding_fallback", True),
            selector=self.selector,
            selection_settings=self.selection_settings,
            analytics=self.analytics,
            router=self.router,
            routing_settings=self.routing_settings,
            policy=self.policy,
            policy_settings=self.policy_settings,
        )
        self.context.add_llm_tools(SendMemeTool())

        self._register_web_apis()

    @filter.command_group("meme")
    def meme_commands():
        """表情包命令组。"""

        pass

    @meme_commands.command("help", alias={"帮助"})
    async def meme_help(self, event: AstrMessageEvent):
        """显示表情包命令帮助。"""

        yield event.plain_result(
            "表情包命令：/meme search 关键词，/meme send 关键词，"
            "/meme list [标签]，/meme refresh，/meme stats"
        )

    def _command_query(
        self, event: AstrMessageEvent, subcommand: str, fallback: str = ""
    ) -> str:
        raw = getattr(event, "message_str", "")
        if isinstance(raw, str):
            parts = raw.strip().split(None, 2)
            if len(parts) >= 3 and parts[1].casefold() == subcommand.casefold():
                return parts[2].strip()[:200]
        return fallback.strip()[:200] if isinstance(fallback, str) else ""

    async def _command_matches(self, query: str, limit: int = 10) -> list[dict]:
        query = query.strip()[:200]
        if not query:
            return []
        if not self._index_is_loaded():
            self._load_index()
        tags = [query]
        try:
            if self.match_mode == "embedding" and self.embedder is not None:
                return await self.matcher.match_embedding(tags, limit=limit, min_score=-1.0)
            if self.match_mode == "hybrid" and self.embedder is not None:
                return await self.matcher.match_hybrid(tags, limit=limit, min_score=0.0)
            return self.matcher.match(tags, limit=limit, min_score=0.0)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 命令搜索失败: {exc}")
            return []

    @staticmethod
    def _command_scope(event: AstrMessageEvent) -> str:
        value = getattr(event, "unified_msg_origin", "global")
        if callable(value):
            value = value()
        return value if isinstance(value, str) and value.strip() else "global"

    @staticmethod
    def _command_summary(matches: list[dict]) -> str:
        lines = []
        for number, item in enumerate(matches[:10], 1):
            filename = item.get("filename", "")
            tags = item.get("tags", [])
            tag_text = ", ".join(tag for tag in tags[:6] if isinstance(tag, str))
            lines.append(f"{number}. {filename} [{tag_text}]")
        return "\n".join(lines)

    @meme_commands.command("search", alias={"搜", "查"})
    async def meme_search(self, event: AstrMessageEvent, query: str = ""):
        """搜索表情包但不发送图片。"""

        query = self._command_query(event, "search", query)
        matches = await self._command_matches(query)
        if not matches:
            yield event.plain_result("没有找到匹配的表情包，请换个关键词。")
            return
        yield event.plain_result(self._command_summary(matches))

    @meme_commands.command("list", alias={"列表"})
    async def meme_list(self, event: AstrMessageEvent, tag: str = ""):
        """列出表情包标签或图片。"""

        tag = self._command_query(event, "list", tag)
        if not self._index_is_loaded():
            self._load_index()
        if not tag:
            tags = self.index.get_unique_tags()[:50]
            yield event.plain_result("可用标签：" + (", ".join(tags) or "暂无"))
            return
        matches = await self._command_matches(tag)
        yield event.plain_result(
            self._command_summary(matches) if matches else "没有找到匹配的表情包。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_commands.command("refresh", alias={"刷新"})
    async def meme_refresh(self, event: AstrMessageEvent):
        """刷新表情包索引。"""

        try:
            report = self._load_index()
            yield event.plain_result(f"表情包索引已刷新，共 {report.get('count', 0)} 张。")
        except Exception:
            yield event.plain_result("刷新失败，请检查来源状态。")

    @meme_commands.command("stats", alias={"统计"})
    async def meme_stats(self, event: AstrMessageEvent):
        """查看发送、策略和路由统计。"""

        analytics = self.analytics.report(5)
        policy = self.policy.status()
        yield event.plain_result(
            "发送统计："
            f"成功 {analytics['totals']['sends']}，失败 {analytics['totals']['failures']}，"
            f"反馈 {analytics['totals']['feedback']}；"
            f"当前配额拒绝 {policy['denied']} 次。"
        )

    @meme_commands.command("send", alias={"发", "发送"})
    async def meme_send(self, event: AstrMessageEvent, query: str = ""):
        """搜索并发送一张表情包。"""

        query = self._command_query(event, "send", query)
        matches = await self._command_matches(query)
        valid = [
            item
            for item in matches
            if isinstance(item.get("path"), str) and Path(item["path"]).is_file()
        ]
        if not valid:
            yield event.plain_result("没有找到可发送的表情包。")
            return
        scope = self._command_scope(event)
        routed, _ = self.router.route(valid, scope=scope)
        decision = self.policy.reserve(routed, scope=scope, query_tags=[query])
        if not decision.allowed:
            yield event.plain_result(f"当前发送策略暂不允许发送（{decision.reason}）。")
            return
        try:
            candidates = self.analytics.personalize(decision.candidates, scope=scope)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 命令个性化排序不可用: {exc}")
            candidates = list(decision.candidates)
        best = self.selector.choose(
            candidates,
            scope=scope,
            settings=self.selection_settings,
        )
        if best is None:
            self.policy.release(scope, decision.reservation_id)
            yield event.plain_result("当前没有可发送的候选。")
            return
        try:
            yield event.image_result(str(best["path"]))
        except Exception as exc:
            self.selector.release(best, scope=scope)
            self.policy.release(scope, decision.reservation_id)
            try:
                self.analytics.record_failure(scope, best.get("id"))
            except Exception as analytics_exc:
                logger.warning(f"[{PLUGIN_NAME}] 命令失败分析记录失败: {analytics_exc}")
            logger.warning(f"[{PLUGIN_NAME}] 命令发送失败: {exc}")
            yield event.plain_result("发送表情包失败，请稍后重试。")
            return
        try:
            self.analytics.record_send(scope, best.get("id"), best.get("tags", []))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 命令发送分析记录失败: {exc}")

    def _load_index(self) -> dict:
        """Load atomically, then invalidate thumbnails only after success."""

        def load_with_metadata() -> dict:
            report = self.index.load()
            try:
                self.catalog.apply(self.index)
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] managed 标签元数据不可用: {exc}")
            return report

        return self._thumbnails.run_index_load(load_with_metadata)

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
            f"/{PLUGIN_NAME}/selection",
            self._api_selection,
            ["GET"],
            "获取表情包选择策略状态",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/analytics",
            self._api_analytics,
            ["GET"],
            "获取发送分析与反馈统计",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/feedback",
            self._api_feedback,
            ["POST"],
            "提交表情包反馈",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/analytics/reset",
            self._api_analytics_reset,
            ["POST"],
            "清理发送分析数据",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/routing",
            self._api_routing,
            ["GET"],
            "获取表情包包与人格路由状态",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/policy",
            self._api_policy,
            ["GET"],
            "获取发送权限与内容策略状态",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/library/import",
            self._api_import,
            ["POST"],
            "导入图片到 managed 表情包库",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/library/tags",
            self._api_update_tags,
            ["POST"],
            "更新 managed 图片标签",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/library/delete",
            self._api_delete,
            ["POST"],
            "删除 managed 图片",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/library/batch",
            self._api_batch,
            ["POST"],
            "批量管理 managed 图片",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/backups",
            self._api_backups,
            ["GET"],
            "列出 managed 库备份",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/backup/create",
            self._api_backup_create,
            ["POST"],
            "创建 managed 库备份",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/backup/restore",
            self._api_backup_restore,
            ["POST"],
            "恢复 managed 库备份",
        )
        ctx.register_web_api(
            f"/{PLUGIN_NAME}/backup/delete",
            self._api_backup_delete,
            ["POST"],
            "删除 managed 库备份",
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

    async def _api_selection(self):
        return json_response(
            {
                "settings": {
                    "mode": self.selection_settings.mode,
                    "pool_size": self.selection_settings.pool_size,
                    "cooldown_seconds": self.selection_settings.cooldown_seconds,
                    "history_size": self.selection_settings.history_size,
                    "deduplicate_files": self.selection_settings.deduplicate_files,
                },
                "status": self.selector.status(),
            }
        )

    async def _api_analytics(self):
        report = self.analytics.report()
        for item in report.get("top_images", []):
            image = self.index.images.get(item.get("id"))
            if isinstance(image, dict):
                item["filename"] = image.get("filename", "")
                item["tags"] = image.get("tags", [])
        return json_response(report)

    async def _api_feedback(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        image_id = body.get("id")
        rating = body.get("rating")
        scope = body.get("scope", "global")
        if not isinstance(image_id, str) or not image_id.strip() or len(image_id) > 512:
            return error_response("id 无效", status_code=400)
        image_id = image_id.strip()
        if isinstance(rating, bool) or not isinstance(rating, int) or rating not in {-1, 1}:
            return error_response("rating 必须是 -1 或 1", status_code=400)
        if not isinstance(scope, str) or len(scope) > 512:
            return error_response("scope 无效", status_code=400)
        if image_id not in self.index.images:
            return error_response("表情包不存在", status_code=404)
        item = self.index.images[image_id]
        tags = item.get("tags", []) if isinstance(item, dict) else []
        if not self.analytics.record_feedback(scope, image_id, rating, tags):
            return error_response("反馈未启用或参数无效", status_code=400)
        return json_response({"status": "ok", "analytics": self.analytics.report(10)})

    async def _api_analytics_reset(self):
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("confirm") != "RESET":
            return error_response("需要 confirm=RESET 才能清理统计", status_code=400)
        if not self.analytics.reset():
            return error_response("清理统计失败", status_code=500)
        return json_response({"status": "ok"})

    async def _api_routing(self):
        return json_response(self.router.status())

    async def _api_policy(self):
        return json_response(self.policy.status())

    def _managed_rel_path(self, image_id: Any) -> str:
        if not isinstance(image_id, str) or image_id not in self.index.images:
            raise CatalogError("表情包不存在")
        item = self.index.images[image_id]
        if not isinstance(item, dict) or item.get("source") != "managed":
            raise CatalogError("仅允许管理 managed 来源图片")
        rel_path = item.get("rel_path")
        if not isinstance(rel_path, str) or not rel_path:
            raise CatalogError("图片路径无效")
        try:
            live_path = self.index.get_abs_path(item)
        except Exception as exc:
            raise CatalogError("图片路径已失效") from exc
        if not live_path.is_file():
            raise CatalogError("图片文件不存在")
        return rel_path

    async def _api_import(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            result = self.catalog.import_base64(
                body.get("filename"), body.get("data_b64"), body.get("tags", [])
            )
            report = self._load_index()
            result["id"] = next(
                (
                    image_id
                    for image_id, item in self.index.images.items()
                    if isinstance(item, dict)
                    and item.get("source") == "managed"
                    and item.get("rel_path") == result["rel_path"]
                ),
                "",
            )
            return json_response({"status": "ok", "item": result, "count": report.get("count", 0)})
        except CatalogError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 导入 managed 图片失败: {exc}")
            return error_response("导入图片失败", status_code=500)

    async def _api_update_tags(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            rel_path = self._managed_rel_path(body.get("id"))
            tags = self.catalog.set_tags(rel_path, body.get("tags", []))
            self._load_index()
            return json_response({"status": "ok", "id": body.get("id"), "tags": tags})
        except CatalogError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 更新 managed 标签失败: {exc}")
            return error_response("更新标签失败", status_code=500)

    async def _api_delete(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            rel_path = self._managed_rel_path(body.get("id"))
            self.catalog.delete_path(rel_path)
            self._load_index()
            return json_response({"status": "ok", "id": body.get("id")})
        except CatalogError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 删除 managed 图片失败: {exc}")
            return error_response("删除图片失败", status_code=500)

    async def _api_batch(self):
        body = await request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("ids"), list):
            return error_response("ids 必须是列表", status_code=400)
        ids = [item for item in body["ids"] if isinstance(item, str)][:100]
        if len(ids) != len(body["ids"]) or not ids:
            return error_response("ids 数量或格式无效", status_code=400)
        action = body.get("action")
        if action not in {"tags", "delete"}:
            return error_response("action 必须是 tags 或 delete", status_code=400)
        if action == "tags":
            try:
                tags = self.catalog.validate_tags(body.get("tags", []))
            except CatalogError as exc:
                return error_response(str(exc), status_code=400)
        else:
            tags = []
        completed: list[str] = []
        errors: list[dict[str, str]] = []
        for image_id in ids:
            try:
                rel_path = self._managed_rel_path(image_id)
                if action == "tags":
                    self.catalog.set_tags(rel_path, tags)
                else:
                    self.catalog.delete_path(rel_path)
                completed.append(image_id)
            except CatalogError as exc:
                errors.append({"id": image_id, "error": str(exc)})
        if completed:
            try:
                self._load_index()
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 批量操作后刷新索引失败")
                return error_response("批量操作完成但刷新索引失败", status_code=500)
        return json_response(
            {"status": "ok", "completed": completed, "errors": errors}
        )

    async def _api_backups(self):
        return json_response({"items": self.backups.list_backups()})

    async def _api_backup_create(self):
        body = await request.json(default={})
        label = body.get("label", "snapshot") if isinstance(body, dict) else "snapshot"
        try:
            return json_response({"status": "ok", "backup": self.backups.create_snapshot(label)})
        except BackupError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_backup_restore(self):
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("confirm") != "RESTORE":
            return error_response("需要 confirm=RESTORE 才能恢复备份", status_code=400)
        try:
            result = self.backups.restore_snapshot(body.get("name"))
            self._load_index()
            return json_response({"status": "ok", **result})
        except BackupError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 恢复备份后刷新索引失败: {exc}")
            return error_response("备份已恢复但刷新索引失败", status_code=500)

    async def _api_backup_delete(self):
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("confirm") != "DELETE":
            return error_response("需要 confirm=DELETE 才能删除备份", status_code=400)
        try:
            self.backups.delete_backup(body.get("name"))
            return json_response({"status": "ok"})
        except BackupError as exc:
            return error_response(str(exc), status_code=400)

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
            "selection_mode": self.config.get("selection_mode", "weighted"),
            "selection_pool_size": self.config.get("selection_pool_size", 5),
            "selection_cooldown_seconds": self.config.get(
                "selection_cooldown_seconds", 300.0
            ),
            "selection_history_size": self.config.get("selection_history_size", 20),
            "deduplicate_files": self.config.get("deduplicate_files", True),
            "analytics_enabled": self.config.get("analytics_enabled", True),
            "analytics_retention_days": self.config.get(
                "analytics_retention_days", 30
            ),
            "personalization_strength": self.config.get(
                "personalization_strength", 0.5
            ),
            "meme_packs": self.config.get("meme_packs", []),
            "default_pack": self.config.get("default_pack", ""),
            "persona_packs": self.config.get("persona_packs", {}),
            "sticky_sessions": self.config.get("sticky_sessions", True),
            "policy_enabled": self.config.get("policy_enabled", True),
            "quota_window_seconds": self.config.get("quota_window_seconds", 60.0),
            "quota_max_sends": self.config.get("quota_max_sends", 8),
            "blocked_tags": self.config.get("blocked_tags", []),
            "allowed_tags": self.config.get("allowed_tags", []),
            "blocked_namespaces": self.config.get("blocked_namespaces", []),
            "blocked_ids": self.config.get("blocked_ids", []),
            "max_file_bytes": self.config.get(
                "max_file_bytes", 20 * 1024 * 1024
            ),
            "backup_retention_count": self.config.get("backup_retention_count", 20),
            "auto_refresh": self.config.get("auto_refresh", True),
            "thumbnail_size": self.config.get("thumbnail_size", 200),
            "library_sources": self.config.get("library_sources", []),
            "embedder_status": "on" if self.embedder is not None else "off",
            "embedding_cache": (
                self.embedder.cache_status if self.embedder is not None else None
            ),
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
        self.selector.clear()
        self.router.clear()
        self.policy.clear()
        await self._thumbnails.close()
