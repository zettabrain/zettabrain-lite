"""
ZettaBrain — Shared retrieval module.

Pipeline per query:
  1. MMR semantic search  (ChromaDB)   — embedding similarity + diversity
  2. BM25 keyword search  (disk index) — exact term matching
  3. Merge + deduplicate               — best of both
  4. Cross-encoder re-rank (FlashRank) — pick the most relevant N

Imports are best-effort: BM25 and re-ranking degrade gracefully to
pure MMR if their packages are missing or the index hasn't been built yet.
"""

import hashlib
import os
import pickle
from pathlib import Path

# ── optional deps ─────────────────────────────────────────────────────────────
try:
    from rank_bm25 import BM25Okapi
    _HAS_BM25 = True
except ImportError:
    _HAS_BM25 = False

try:
    import io as _io, os as _os, sys as _sys
    # Suppress onnxruntime C++ warnings (write to OS fd 2) and Python logging noise.
    # Strategy: redirect Python sys.stderr to StringIO (so any logging.StreamHandler
    # installed during import has a live buffer, not a file that gets closed later),
    # AND dup fd 2 to /dev/null (so C++ code writing directly to stderr is silenced).
    _os.environ.setdefault("ORT_LOGGING_LEVEL", "3")  # 3 = ERROR only
    _saved_stderr = _sys.stderr
    _sys.stderr = _io.StringIO()        # Python logging → buffer, never closed
    _saved_fd2  = _os.dup(2)            # save real OS-level stderr fd
    _devnull_fd = _os.open(_os.devnull, _os.O_WRONLY)
    _os.dup2(_devnull_fd, 2)            # C++ writes → /dev/null
    _os.close(_devnull_fd)
    try:
        from flashrank import Ranker, RerankRequest
        _ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="/tmp/flashrank")
        _HAS_RERANKER = True
    finally:
        _os.dup2(_saved_fd2, 2)         # restore OS-level stderr
        _os.close(_saved_fd2)
        _sys.stderr = _saved_stderr     # restore Python stderr (StringIO left open — safe)
except Exception:
    _ranker = None
    _HAS_RERANKER = False

# ── paths ─────────────────────────────────────────────────────────────────────
def _chroma_parent() -> Path:
    """Locate the ChromaDB parent dir from env / config file / fallback."""
    chroma = os.environ.get("ZETTABRAIN_CHROMA", "")
    if not chroma:
        for cfg_path in ["/opt/zettabrain/src/zettabrain.env"]:
            if os.path.exists(cfg_path):
                for line in open(cfg_path):
                    if line.startswith("ZETTABRAIN_CHROMA="):
                        chroma = line.split("=", 1)[1].strip().strip('"')
    return Path(chroma or "/opt/zettabrain/src/zettabrain_vectorstore").parent

BM25_PATH = _chroma_parent() / "bm25_index.pkl"

# ── improved prompt ───────────────────────────────────────────────────────────
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


def format_context(docs) -> str:
    parts = []
    for doc in docs:
        source = Path(doc.metadata.get("source", "unknown")).name
        page   = doc.metadata.get("page", "")
        label  = f"{source} p.{page}" if page != "" else source
        parts.append(f"[{label}]\n{doc.page_content}")
    return "\n\n---\n\n".join(parts)


# ── BM25 ──────────────────────────────────────────────────────────────────────
def _load_bm25_data() -> dict | None:
    if not _HAS_BM25 or not BM25_PATH.exists():
        return None
    try:
        with open(BM25_PATH, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _bm25_search(query: str, k: int = 12):
    from langchain_core.documents import Document
    data = _load_bm25_data()
    if not data:
        return []
    tokens = query.lower().split()
    scores = data["bm25"].get_scores(tokens)
    top    = scores.argsort()[-(min(k, len(scores))):][::-1]
    return [
        Document(page_content=data["docs"][i], metadata=data["metadatas"][i])
        for i in top if scores[i] > 0
    ]


def rebuild_bm25_index(vectorstore) -> int:
    """Rebuild BM25 index from all documents in ChromaDB. Call after ingestion."""
    if not _HAS_BM25:
        return 0
    try:
        result    = vectorstore._collection.get(include=["documents", "metadatas"])
        docs      = result["documents"]
        metadatas = result["metadatas"]
        if not docs:
            return 0
        bm25 = BM25Okapi([d.lower().split() for d in docs])
        BM25_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BM25_PATH, "wb") as f:
            pickle.dump({"bm25": bm25, "docs": docs, "metadatas": metadatas}, f)
        return len(docs)
    except Exception:
        return 0


# ── query normalisation ───────────────────────────────────────────────────────
# Words that carry no topical signal in short question queries.
_QUESTION_STOPWORDS = frozenset({
    "what", "is", "are", "was", "were", "be", "been", "being",
    "how", "does", "do", "did", "can", "could", "would", "should",
    "the", "a", "an", "of", "in", "to", "for", "and", "or", "not",
    "with", "about", "explain", "describe", "tell", "me", "give",
    "show", "define", "meaning", "please", "help", "understand",
    "overview", "summary", "summarise", "summarize",
})


def _core_terms(question: str) -> str | None:
    """
    Strip question words from short queries to produce a keyword-search-
    friendly core.  Returns None when nothing useful was stripped (e.g. the
    query is already a bare keyword, or is long enough to be self-describing).

    Examples
    --------
    "What is AWS?"              → "aws"
    "How does NFS work?"        → "nfs work"
    "Amazon Web Services"       → None  (no stopwords removed)
    "Explain deep learning"     → "deep learning"
    "What are the key features of ChromaDB and vector databases?" → None (>8 words)
    """
    words = question.lower().translate(str.maketrans("", "", "?!.,;:\"'")).split()
    if len(words) > 8:
        return None  # long queries are specific enough already
    core = [w for w in words if w not in _QUESTION_STOPWORDS and len(w) > 1]
    if not core or set(core) == set(words):
        return None  # nothing useful was stripped
    return " ".join(core)


# ── hybrid retrieval ──────────────────────────────────────────────────────────
def hybrid_retrieve(question: str, vectorstore, top_k: int = 5) -> list:
    """
    Retrieve the top_k most relevant chunks for a question.

    1. MMR semantic search  — fetch 30 candidates, return 6 (relevance-focused)
    2. BM25 keyword search  — fetch 10 candidates with original question
    3. BM25 keyword search  — fetch 8 candidates with core terms only
       (strips question words so "What is AWS?" → "aws" → hits the right doc)
    4. Deduplicate by content hash
    5. Re-rank with FlashRank cross-encoder → return top_k

    MMR tuning: lambda_mult=0.82 keeps results tightly on-topic.
    """
    # 1. semantic (MMR) — relevance-first, modest diversity
    semantic = vectorstore.as_retriever(
        search_type="mmr",
        search_kwargs={"k": 6, "fetch_k": 30, "lambda_mult": 0.82},
    ).invoke(question)

    # 2. BM25 with original question
    keyword = _bm25_search(question, k=10)

    # 3. BM25 with core terms (handles acronym / stopword-heavy questions)
    core = _core_terms(question)
    keyword_core = _bm25_search(core, k=8) if core else []

    # 4. merge + deduplicate (semantic results ranked first)
    seen, merged = set(), []
    for doc in semantic + keyword + keyword_core:
        key = hashlib.md5(doc.page_content.encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            merged.append(doc)

    # 5. re-rank with original question so relevance judgement is correct
    if _HAS_RERANKER and len(merged) > top_k:
        try:
            passages = [{"id": i, "text": d.page_content} for i, d in enumerate(merged)]
            ranked   = _ranker.rerank(RerankRequest(query=question, passages=passages))
            return [merged[r["id"]] for r in ranked[:top_k]]
        except Exception:
            pass

    return merged[:top_k]
