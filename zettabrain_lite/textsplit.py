"""Recursive character text splitting.

Replaces langchain-text-splitters. Splits on the largest natural boundary that fits —
paragraph, then line, then sentence, then word — so chunks break where a reader would.
"""

from __future__ import annotations

_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ", ""]


def _split_on(text: str, separator: str) -> list[str]:
    if separator == "":
        return list(text)
    parts = text.split(separator)
    # Keep the separator on the preceding piece so rejoining preserves the original text.
    return [p + separator for p in parts[:-1]] + parts[-1:]


def split_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 150) -> list[str]:
    """Split text into chunks of at most chunk_size characters, overlapping by chunk_overlap."""
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    pieces = _atomic_pieces(text, chunk_size)
    return _merge(pieces, chunk_size, chunk_overlap)


def _atomic_pieces(text: str, chunk_size: int) -> list[str]:
    """Break text down until every piece fits in chunk_size."""
    for i, separator in enumerate(_SEPARATORS):
        parts = _split_on(text, separator)
        if len(parts) == 1:
            continue
        out: list[str] = []
        for part in parts:
            if len(part) <= chunk_size or i == len(_SEPARATORS) - 1:
                out.append(part)
            else:
                out.extend(_atomic_pieces(part, chunk_size))
        return out
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _merge(pieces: list[str], chunk_size: int, chunk_overlap: int) -> list[str]:
    """Recombine small pieces into chunks, carrying an overlap between them."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for piece in pieces:
        if length + len(piece) > chunk_size and current:
            chunks.append("".join(current).strip())
            # Carry the tail of this chunk into the next so context is not cut mid-thought.
            kept: list[str] = []
            kept_len = 0
            for previous in reversed(current):
                if kept_len + len(previous) > chunk_overlap:
                    break
                kept.insert(0, previous)
                kept_len += len(previous)
            current, length = kept, kept_len
        current.append(piece)
        length += len(piece)

    if current:
        tail = "".join(current).strip()
        if tail:
            chunks.append(tail)
    return [c for c in chunks if c]
