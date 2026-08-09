"""Backend package with lazy public exports.

Keeping imports lazy lets the pure indexing and validation modules be used by
maintenance scripts and tests without importing the AstrBot runtime first.
"""

from __future__ import annotations

from typing import Any


__all__ = [
    "MemeIndex",
    "TagMatcher",
    "SendMemeTool",
    "MemeEmbedder",
    "MemeSelector",
    "SelectionSettings",
    "MemeAnalytics",
    "AnalyticsSettings",
    "MemeRouter",
    "RoutingSettings",
    "MemePolicy",
    "PolicySettings",
    "MemeSender",
    "SendPipelineSettings",
    "EmotionAgentRuntime",
    "EmotionAgentSettings",
    "EmotionDelegationTool",
    "QueryPlan",
    "build_query_plan",
    "ManagedCatalog",
    "CatalogError",
    "BackupManager",
    "BackupError",
]


def __getattr__(name: str) -> Any:
    if name == "MemeIndex":
        from .index import MemeIndex

        return MemeIndex
    if name == "TagMatcher":
        from .matcher import TagMatcher

        return TagMatcher
    if name == "SendMemeTool":
        from .tool import SendMemeTool

        return SendMemeTool
    if name == "MemeEmbedder":
        from .embedder import MemeEmbedder

        return MemeEmbedder
    if name in {"MemeSelector", "SelectionSettings"}:
        from .selector import MemeSelector, SelectionSettings

        return {"MemeSelector": MemeSelector, "SelectionSettings": SelectionSettings}[name]
    if name in {"MemeAnalytics", "AnalyticsSettings"}:
        from .analytics import AnalyticsSettings, MemeAnalytics

        return {"MemeAnalytics": MemeAnalytics, "AnalyticsSettings": AnalyticsSettings}[name]
    if name in {"MemeRouter", "RoutingSettings"}:
        from .routing import MemeRouter, RoutingSettings

        return {"MemeRouter": MemeRouter, "RoutingSettings": RoutingSettings}[name]
    if name in {"MemePolicy", "PolicySettings"}:
        from .policy import MemePolicy, PolicySettings

        return {"MemePolicy": MemePolicy, "PolicySettings": PolicySettings}[name]
    if name in {"MemeSender", "SendPipelineSettings"}:
        from .sender import MemeSender, SendPipelineSettings

        return {"MemeSender": MemeSender, "SendPipelineSettings": SendPipelineSettings}[name]
    if name in {"EmotionAgentRuntime", "EmotionAgentSettings", "EmotionDelegationTool"}:
        from .emotion_agent import (
            EmotionAgentRuntime,
            EmotionAgentSettings,
            EmotionDelegationTool,
        )

        return {
            "EmotionAgentRuntime": EmotionAgentRuntime,
            "EmotionAgentSettings": EmotionAgentSettings,
            "EmotionDelegationTool": EmotionDelegationTool,
        }[name]
    if name in {"QueryPlan", "build_query_plan"}:
        from .query import QueryPlan, build_query_plan

        return {"QueryPlan": QueryPlan, "build_query_plan": build_query_plan}[name]
    if name in {"ManagedCatalog", "CatalogError"}:
        from .catalog import CatalogError, ManagedCatalog

        return {"ManagedCatalog": ManagedCatalog, "CatalogError": CatalogError}[name]
    if name in {"BackupManager", "BackupError"}:
        from .backup import BackupError, BackupManager

        return {"BackupManager": BackupManager, "BackupError": BackupError}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
