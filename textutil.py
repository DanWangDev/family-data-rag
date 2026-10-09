"""textutil.py — shared text helpers: jieba segmentation for FTS, chunker."""

import jieba


def segment(text: str) -> str:
    """FTS5's default tokenizer can't segment CJK; store and query jieba-segmented text."""
    return " ".join(jieba.cut(text, cut_all=False))


def split_text(text: str, *, target: int = 1000, overlap: int = 150) -> list[str]:
    """Split into ~target-char windows with overlap; backs off to a newline near the edge."""
    if len(text) <= target:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + target, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + int(target * 0.7), end)
            if nl > start:
                end = nl + 1
        parts.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return parts
