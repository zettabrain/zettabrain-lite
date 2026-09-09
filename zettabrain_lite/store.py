"""Document store: sqlite-vec for vectors, FTS5 for keywords, one file on disk.

Replaces ChromaDB and rank-bm25. Both indexes live in the same SQLite database, so a corpus
is a single file that can be copied, backed up or shipped to a server. The price list already
uses FTS5 in this codebase; this puts the corpus on the same footing.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import struct
from pathlib import Path
from typing import Iterable, Optional

from .documents import Document

log = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source   TEXT NOT NULL,
    filename TEXT NOT NULL,
    page     INTEGER DEFAULT 0,
    content  TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_filename ON chunks(filename);
CREATE INDEX IF NOT EXISTS idx_chunks_source   ON chunks(source);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _pack(vector: Iterable[float]) -> bytes:
    values = list(vector)
    return struct.pack(f"{len(values)}f", *values)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _fts_query(text: str) -> str:
    """Turn free text into an FTS5 MATCH expression.

    Every token is quoted, so punctuation in a user's question cannot be read as FTS5
    operator syntax and raise. Tokens are OR-ed because a document matching some of the
    query terms is still a candidate — ranking sorts out how good it is.
    """
    tokens = re.findall(r"[\w']+", text.lower())
    tokens = [t for t in tokens if len(t) > 1]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


class DocumentStore:
    """Chunk storage with a vector index and a keyword index."""

    def __init__(self, db_path: str | Path, dimension: int = 0):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._dimension = dimension
        self._conn: Optional[sqlite3.Connection] = None

    # ── connection ───────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            import sqlite_vec  # noqa: PLC0415

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "The vector search extension could not be loaded. Reinstall ZettaBrain, "
                "or run: pip install sqlite-vec"
            ) from exc
        conn.executescript(_SCHEMA)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
            self._ensure_vector_table()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── vector table ─────────────────────────────────────────────────────────

    def _stored_dimension(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'dimension'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _ensure_vector_table(self) -> None:
        """Create the vector table, rebuilding it if the embedding size changed.

        Vector tables are declared with a fixed width. Switching embedding model changes that
        width, and the stored vectors are meaningless under a different model anyway, so the
        index is dropped and the corpus must be re-ingested.
        """
        stored = self._stored_dimension()
        if not self._dimension:
            self._dimension = stored
        if not self._dimension:
            return  # nothing indexed yet and no embedder configured

        if stored and stored != self._dimension:
            log.warning(
                "Embedding size changed from %d to %d — clearing the vector index", stored, self._dimension
            )
            self._conn.execute("DROP TABLE IF EXISTS chunks_vec")
            self._conn.execute("DELETE FROM chunks")

        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{self._dimension}])"
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO store_meta(key, value) VALUES ('dimension', ?)",
            (str(self._dimension),),
        )
        self._conn.commit()

    # ── writes ───────────────────────────────────────────────────────────────

    def add(self, documents: list[Document], vectors: list[list[float]]) -> int:
        """Store chunks with their embeddings. Returns the number written."""
        if not documents:
            return 0
        if len(documents) != len(vectors):
            raise ValueError("documents and vectors must be the same length")
        if not self._dimension:
            self._dimension = len(vectors[0])
            self._ensure_vector_table()

        conn = self.conn
        written = 0
        for doc, vector in zip(documents, vectors):
            source = str(doc.metadata.get("source", ""))
            cursor = conn.execute(
                "INSERT INTO chunks (source, filename, page, content, metadata) VALUES (?,?,?,?,?)",
                (
                    source,
                    doc.metadata.get("filename") or Path(source).name,
                    _as_int(doc.metadata.get("page")),
                    doc.page_content,
                    json.dumps(doc.metadata, default=str),
                ),
            )
            conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (cursor.lastrowid, _pack(vector)),
            )
            written += 1
        conn.commit()
        return written

    def delete_source(self, source: str) -> int:
        """Remove every chunk from one file, by full path or by filename."""
        conn = self.conn
        rows = conn.execute(
            "SELECT id FROM chunks WHERE source = ? OR filename = ?", (source, Path(source).name)
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (row["id"],))
        conn.execute(
            "DELETE FROM chunks WHERE source = ? OR filename = ?", (source, Path(source).name)
        )
        conn.commit()
        return len(rows)

    def clear(self) -> None:
        conn = self.conn
        conn.execute("DELETE FROM chunks")
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.execute("DELETE FROM store_meta WHERE key = 'dimension'")
        conn.commit()
        self._ensure_vector_table()

    # ── reads ────────────────────────────────────────────────────────────────

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])

    def sources(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT source FROM chunks ORDER BY source"
        ).fetchall()
        return [r["source"] for r in rows]

    def _to_document(self, row: sqlite3.Row) -> Document:
        try:
            metadata = json.loads(row["metadata"])
        except Exception:
            metadata = {}
        metadata.setdefault("source", row["source"])
        metadata.setdefault("filename", row["filename"])
        metadata.setdefault("page", row["page"])
        return Document(page_content=row["content"], metadata=metadata)

    def all_documents(self) -> list[Document]:
        rows = self.conn.execute(
            "SELECT * FROM chunks ORDER BY filename, page, id"
        ).fetchall()
        return [self._to_document(r) for r in rows]

    def documents_for_files(self, filenames: list[str]) -> list[Document]:
        """Every chunk from the named files, in reading order."""
        if not filenames:
            return []
        placeholders = ",".join("?" * len(filenames))
        rows = self.conn.execute(
            f"SELECT * FROM chunks WHERE filename IN ({placeholders}) ORDER BY filename, page, id",
            filenames,
        ).fetchall()
        return [self._to_document(r) for r in rows]

    def search_vector(
        self, query_vector: list[float], k: int = 10, with_vectors: bool = False
    ) -> list[tuple[Document, float]] | list[tuple[Document, float, list[float]]]:
        """Nearest neighbours, closest first.

        Returns (document, distance), or (document, distance, embedding) when with_vectors is
        set — MMR needs each candidate's own vector to measure redundancy between them.
        """
        if not self._dimension:
            return []
        columns = "c.*, v.distance AS distance" + (", v.embedding AS embedding" if with_vectors else "")
        try:
            rows = self.conn.execute(
                f"""
                SELECT {columns}
                FROM chunks_vec v
                JOIN chunks c ON c.id = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
                """,
                (_pack(query_vector), k),
            ).fetchall()
        except sqlite3.Error:
            log.debug("Vector search failed", exc_info=True)
            return []
        if not with_vectors:
            return [(self._to_document(r), float(r["distance"])) for r in rows]
        return [
            (self._to_document(r), float(r["distance"]), _unpack(r["embedding"]))
            for r in rows
        ]

    def search_keyword(self, query: str, k: int = 10) -> list[tuple[Document, float]]:
        """BM25 keyword search. Returns (document, score), best first."""
        match = _fts_query(query)
        if not match:
            return []
        try:
            rows = self.conn.execute(
                """
                SELECT c.*, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (match, k),
            ).fetchall()
        except sqlite3.Error:
            log.debug("Keyword search failed for %r", query, exc_info=True)
            return []
        # bm25() returns a negative score where more negative is better; flip it so callers
        # can treat larger as better without knowing that.
        return [(self._to_document(r), -float(r["score"])) for r in rows]


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
