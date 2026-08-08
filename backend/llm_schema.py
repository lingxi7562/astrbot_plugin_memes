"""Small, explicit schemas for the plugin's LLM-facing tools."""

from __future__ import annotations


def send_meme_parameters() -> dict:
    """Return a low-decision-count schema for the ``send_meme`` tool.

    ``intent`` is the happy path: the model can describe the desired reaction
    in one short sentence.  The legacy fields remain accepted for existing
    callers, but are deliberately described as advanced/optional controls.
    """

    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "maxLength": 256,
                "description": (
                    "首选字段：用一句短话描述想表达的感觉或回应。"
                    "例如‘对方讲冷笑话，我想无语吐槽’。通常只填 intent。"
                ),
            },
            "tags": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string", "maxLength": 64},
                "description": (
                    "兼容旧调用；只有不方便写 intent 时才填 1-4 个情绪/内容词。"
                ),
            },
            "scene": {
                "type": "string",
                "maxLength": 200,
                "description": "兼容字段，通常留空；用于补充特殊场景。",
            },
            "pack": {
                "type": "string",
                "maxLength": 32,
                "description": "高级选项，通常留空；指定表情包包 ID。",
            },
            "persona": {
                "type": "string",
                "maxLength": 64,
                "description": "高级选项，通常留空；指定人格别名。",
            },
        },
        # An empty call is intentional: the implementation can use the active
        # message as a safe fallback when a model omits arguments under load.
        "required": [],
        "additionalProperties": False,
    }


__all__ = ["send_meme_parameters"]
