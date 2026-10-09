"""textutil.py — shared text helpers: jieba segmentation for FTS, chunker."""

import jieba


def segment(text: str) -> str:
    """FTS5's default tokenizer can't segment CJK; store and query jieba-segmented text."""
    return " ".join(jieba.cut(text, cut_all=False))


def split_text(text: str, *, target: int = 1000, overlap: int = 150) -> list[str]:
    """Split into ~target-char windows with overlap; backs off to a newline near the edge."""
    return [text[start:end] for start, end in split_text_spans(text, target=target, overlap=overlap)]


def split_text_spans(text: str, *, target: int = 1000, overlap: int = 150) -> list[tuple[int, int]]:
    """Return the original offsets directly, including for repeated/overlapping text."""
    if target <= 0 or not 0 <= overlap < target:
        raise ValueError("Require target > 0 and 0 <= overlap < target")
    if len(text) <= target:
        return [(0, len(text))]
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + target, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + int(target * 0.7), end)
            if nl > start:
                end = nl + 1
        spans.append((start, end))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return spans
