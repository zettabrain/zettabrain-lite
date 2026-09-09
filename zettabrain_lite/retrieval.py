"""
ZettaBrain — Shared retrieval module.

Pipeline per query:
  1. Vector search (sqlite-vec)  — embedding similarity, then MMR for diversity
  2. Keyword search (SQLite FTS5) — exact term matching, BM25 ranked
  3. Merge with Reciprocal Rank Fusion — a chunk both methods like ranks highest
  4. Return the top N

Both indexes live in one SQLite file. There is no separate vector database, no pickled
BM25 index and no cross-encoder model to download at runtime.
"""

from __future__ import annotations

import hashlib
import logging
import math
from pathlib import Path

from .documents import Document

log = logging.getLogger(__name__)

# ── prompts ───────────────────────────────────────────────────────────────────

RAG_PROMPT = """You are ZettaBrain, an expert assistant that answers questions from a private document library.

Rules:
- Answer ONLY from the CONTEXT provided. Do not use outside knowledge.
- After each key fact, cite the source filename in brackets, e.g. [report.pdf].
- If the query is a single word, acronym, or short topic (e.g. "AWS", "NFS", "RAG"), treat it as "give me an overview of this topic" and summarise everything the context says about it.
- If the context partially answers the question, give what you can and note the gap.
- If the context has no relevant information at all, say: "I don't have information about this topic in the document library."
- Be concise but complete. Use bullet points for multi-part answers.

CONTEXT:
{context}

QUESTION: {question}

ANSWER:"""

ADVANCED_RAG_PROMPT = """You are ZettaBrain, an expert AI assistant grounded in a private document library.

Your task: Answer the question below using ONLY the context provided. Every factual claim must cite its source.

Rules:
- Base your answer ONLY on the CONTEXT below. Never use outside knowledge.
- After each fact, cite the source in brackets: [filename.pdf] or [filename.pdf p.3]
- If the context partially answers the question, provide what you can and note what's missing.
- If the context has no relevant information, say: "I don't have information about this in the document library."
- Structure your answer clearly with bullet points for multi-part responses.
- Be thorough but concise — cover all relevant points from the sources.

CONTEXT:
{context}

QUESTION: {question}

ANSWER (with inline citations):"""


def format_context(docs: list[Document]) -> str:
    parts = []
    for doc in docs:
        source = Path(doc.metadata.get("source", "unknown")).name
        page = doc.metadata.get("page", "")
        label = f"{source} p.{page}" if page != "" else source
        parts.append(f"[{label}]\n{doc.page_content}")
    return "\n\n---\n\n".join(parts)


# ── query normalisation ───────────────────────────────────────────────────────

_QUESTION_STOPWORDS = frozenset({
    "what", "is", "are", "was", "were", "be", "been", "being", "how", "does", "do", "did",
    "can", "could", "would", "should", "the", "a", "an", "of", "in", "to", "for", "and",
    "or", "not", "with", "about", "explain", "describe", "tell", "me", "give", "show",
    "define", "meaning", "please", "help", "understand", "overview", "summary",
    "summarise", "summarize",
})


def _core_terms(question: str) -> str | None:
    """Strip question words from a short query to leave a keyword-search-friendly core.

    "What is AWS?" -> "aws". Returns None when nothing useful was removed or the query is
    already long enough to be specific.
    """
    words = question.lower().translate(str.maketrans("", "", "?!.,;:\"'")).split()
    if len(words) > 8:
        return None
    core = [w for w in words if w not in _QUESTION_STOPWORDS and len(w) > 1]
    if not core or set(core) == set(words):
        return None
    return " ".join(core)


# ── fusion and diversity ──────────────────────────────────────────────────────


def _key(doc: Document) -> str:
    return hashlib.md5(doc.page_content.encode()).hexdigest()


def _rrf_merge(ranked_lists: list[list[Document]], k: int = 60) -> list[Document]:
    """Reciprocal Rank Fusion: a chunk ranked well by several methods rises to the top."""
    scores: dict[str, list] = {}
    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked):
            entry = scores.setdefault(_key(doc), [0.0, doc])
            entry[0] += 1.0 / (k + rank + 1)
    return [doc for _, doc in sorted(scores.values(), key=lambda pair: -pair[0])]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _mmr(
    query_vector: list[float],
    candidates: list[tuple[Document, list[float]]],
    k: int,
    lambda_mult: float = 0.82,
) -> list[Document]:
    """Maximal Marginal Relevance: pick results that are relevant but not near-duplicates.

    Chroma did this internally. Written out here it is a few lines, and it matters for a
    corpus where the same passage appears in several documents.
    """
    if not candidates:
        return []
    selected: list[tuple[Document, list[float]]] = []
    remaining = list(candidates)

    while remaining and len(selected) < k:
        best_index, best_score = 0, -math.inf
        for i, (_, vector) in enumerate(remaining):
            relevance = _cosine(query_vector, vector)
            redundancy = max((_cosine(vector, chosen) for _, chosen in selected), default=0.0)
            score = lambda_mult * relevance - (1 - lambda_mult) * redundancy
            if score > best_score:
                best_index, best_score = i, score
        selected.append(remaining.pop(best_index))
    return [doc for doc, _ in selected]


# ── hybrid retrieval ──────────────────────────────────────────────────────────


def hybrid_retrieve(question: str, store, top_k: int = 5, embedder=None) -> list[Document]:
    """Retrieve the top_k most relevant chunks.

    1. Vector search for 30 candidates, reduced to 6 by MMR
    2. Keyword search on the question, and again on its core terms
    3. Reciprocal Rank Fusion across all three lists
    """
    ranked_lists: list[list[Document]] = []

    if embedder is not None:
        try:
            query_vector = embedder.embed_query(question)
            hits = store.search_vector(query_vector, k=30, with_vectors=True)
            candidates = [(doc, vector) for doc, _distance, vector in hits]
            ranked_lists.append(_mmr(query_vector, candidates, k=6))
        except Exception as exc:
            log.warning("Vector search unavailable, falling back to keyword search: %s", exc)

    keyword = [doc for doc, _ in store.search_keyword(question, k=10)]
    if keyword:
        ranked_lists.append(keyword)

    core = _core_terms(question)
    if core:
        keyword_core = [doc for doc, _ in store.search_keyword(core, k=8)]
        if keyword_core:
            ranked_lists.append(keyword_core)

    if not ranked_lists:
        return []
    return _rrf_merge(ranked_lists)[:top_k]


# ── Advanced RAG ─────────────────────────────────────────────────────────────


def expand_queries(question: str, llm_fn=None) -> list[str]:
    """Stage 1: ask the model for several search phrasings of one question."""
    if not llm_fn:
        return [question]

    prompt = (
        "Given this user question, generate exactly 3 search queries to find relevant documents.\n"
        "Return ONLY the queries, one per line, no numbering or labels.\n\n"
        "Line 1: Rephrase the question in natural language (semantic search)\n"
        "Line 2: Extract key terms, acronyms, and entities (keyword search)\n"
        "Line 3: Broaden to related concepts that might contain the answer\n\n"
        f"Question: {question}"
    )
    try:
        lines = [line.strip() for line in llm_fn(prompt).strip().split("\n") if line.strip()]
        if lines:
            return lines[:3]
    except Exception:
        log.debug("Query expansion failed", exc_info=True)
    return [question]


def select_chunks(question: str, chunks: list[Document], llm_fn, max_select: int = 8) -> list[Document]:
    """Stage 3: let the model choose the most relevant chunks."""
    if not llm_fn or len(chunks) <= max_select:
        return chunks[:max_select]

    numbered = "\n\n".join(f"[{i + 1}] {doc.page_content[:300]}" for i, doc in enumerate(chunks[:20]))
    prompt = (
        "You are evaluating document chunks for relevance to a user's question.\n"
        f"Below are {min(len(chunks), 20)} chunks retrieved from a document library. "
        "Select the chunk numbers that are most relevant to answering the question. "
        "Return ONLY the numbers, comma-separated.\n\n"
        f"Question: {question}\n\n{numbered}\n\n"
        "Most relevant chunk numbers (comma-separated):"
    )
    try:
        import re  # noqa: PLC0415

        numbers = [int(n) for n in re.findall(r"\d+", llm_fn(prompt))]
        selected: list[Document] = []
        for n in numbers:
            index = n - 1
            if 0 <= index < len(chunks) and chunks[index] not in selected:
                selected.append(chunks[index])
            if len(selected) >= max_select:
                break
        if selected:
            return selected
    except Exception:
        log.debug("Chunk selection failed", exc_info=True)
    return chunks[:max_select]


def expand_context(selected: list[Document], store, window: int = 1) -> list[Document]:
    """Stage 4: include the chunks on either side of each selection, from the same file."""
    try:
        everything = store.all_documents()
    except Exception:
        return selected
    if not everything:
        return selected

    by_file: dict[str, list[Document]] = {}
    for doc in everything:
        by_file.setdefault(doc.metadata.get("filename", ""), []).append(doc)

    expanded: list[Document] = []
    seen: set[str] = set()
    for chunk in selected:
        siblings = by_file.get(chunk.metadata.get("filename", ""), [])
        try:
            position = siblings.index(chunk)
        except ValueError:
            position = None
        if position is None:
            candidates = [chunk]
        else:
            low = max(0, position - window)
            candidates = siblings[low : position + window + 1]
        for doc in candidates:
            key = _key(doc)
            if key not in seen:
                seen.add(key)
                expanded.append(doc)
    return expanded


def advanced_retrieve(question: str, store, top_k: int = 5, llm_fn=None, embedder=None) -> list[Document]:
    """Multi-query retrieval: expand, search, fuse, select, widen."""
    ranked_lists: list[list[Document]] = []
    for query in expand_queries(question, llm_fn):
        ranked_lists.append(hybrid_retrieve(query, store, top_k=top_k * 2, embedder=embedder))

    merged = _rrf_merge([lst for lst in ranked_lists if lst])
    if not merged:
        return []

    selected = (
        select_chunks(question, merged, llm_fn, max_select=top_k * 2)
        if llm_fn and len(merged) > top_k
        else merged[: top_k * 2]
    )
    return expand_context(selected, store, window=1)[:top_k]


def rebuild_bm25_index(store) -> int:
    """Kept for callers that still invoke it. FTS5 indexes on write, so this is a no-op."""
    try:
        return store.count()
    except Exception:
        return 0
