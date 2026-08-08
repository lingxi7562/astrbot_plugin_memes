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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
