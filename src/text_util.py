"""Shared text utilities — Traditional→Simplified conversion + method labels."""

from __future__ import annotations

from zhconv import convert as _zhconv_convert


def to_simplified(s: str | None) -> str:
    """Convert Traditional Chinese characters to Simplified. Safe on None/empty/non-CJK."""
    if not s:
        return ""
    try:
        return _zhconv_convert(s, "zh-cn")
    except Exception:
        return s


# Human-readable labels for matcher `method` codes.
_METHOD_LABELS = {
    "isrc": "ISRC 完全一致",
    "title+artist+duration": "标题 + 歌手 + 时长 全对上",
    "title+artist": "标题 + 歌手 对上（时长不同）",
    "title+duration": "标题 + 时长 对上（歌手不同）",
    "artist+duration": "歌手 + 时长 对上（标题不同）",
    "title": "只对上标题",
    "artist": "只对上歌手",
    "duration": "只对上时长",
    "none": "全都对不上",
}


def explain_method(method: str) -> str:
    """Return a human-readable reason string for a matcher method code.

    Handles the `title-only|<inner>` prefix from the fallback path by
    translating the inner method and tagging it with `兜底`.
    """
    if not method:
        return "未知"
    if method.startswith("title-only|"):
        inner = method.split("|", 1)[1]
        base = _METHOD_LABELS.get(inner, inner)
        return f"title-only 兜底: {base}"
    return _METHOD_LABELS.get(method, method)
