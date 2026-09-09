"""Unit tests for the sqlite-vec document store and hybrid retrieval."""

import math

import pytest

from zettabrain_lite.documents import Document
from zettabrain_lite.retrieval import _core_terms, _mmr, _rrf_merge, hybrid_retrieve
from zettabrain_lite.store import DocumentStore, _fts_query
from zettabrain_lite.textsplit import split_text

DIM = 8


def _vec(seed: int) -> list[float]:
    """A deterministic unit-ish vector, distinct per seed."""
    return [math.sin(seed + i) for i in range(DIM)]


class _StubEmbedder:
    """Maps known phrases to fixed vectors so retrieval is testable without a model."""

    dimension = DIM

    def __init__(self, mapping: dict[str, int]):
        self.mapping = mapping

    def _for(self, text: str) -> list[float]:
        for phrase, seed in self.mapping.items():
            if phrase in text.lower():
                return _vec(seed)
        return _vec(99)

    def embed_documents(self, texts):
        return [self._for(t) for t in texts]

    def embed_query(self, text):
        return self._for(text)


@pytest.fixture
def store(tmp_path):
    s = DocumentStore(tmp_path / "corpus.db", dimension=DIM)
    yield s
    s.close()


DOCS = [
    Document("The corporate account discount is 12 percent.",
             {"source": "/docs/terms.pdf", "filename": "terms.pdf", "page": 1}),
    Document("Delivery to the island is charged per drop.",
             {"source": "/docs/terms.pdf", "filename": "terms.pdf", "page": 2}),
    Document("Bouquets are arranged fresh each morning.",
             {"source": "/docs/about.md", "filename": "about.md", "page": 0}),
]
VECTORS = [_vec(1), _vec(2), _vec(3)]


class TestDocumentStore:
    def test_add_and_count(self, store):
        assert store.add(DOCS, VECTORS) == 3
        assert store.count() == 3

    def test_round_trip_preserves_metadata(self, store):
        store.add(DOCS, VECTORS)
        by_name = {d.metadata["filename"]: d for d in store.all_documents()}
        assert by_name["terms.pdf"].metadata["source"] == "/docs/terms.pdf"
        assert by_name["about.md"].metadata["page"] == 0

    def test_keyword_search_finds_the_right_chunk(self, store):
        store.add(DOCS, VECTORS)
        hits = store.search_keyword("corporate discount", k=3)
        assert hits
        assert "12 percent" in hits[0][0].page_content

    def test_keyword_search_survives_punctuation(self, store):
        """A user's question is passed straight in; FTS5 syntax must not leak."""
        store.add(DOCS, VECTORS)
        for query in ['What is the "corporate" discount?', "delivery -- island (per drop)?", "AND OR NOT"]:
            store.search_keyword(query, k=3)  # must not raise

    def test_vector_search_orders_by_distance(self, store):
        store.add(DOCS, VECTORS)
        hits = store.search_vector(_vec(2), k=3)
        assert "Delivery" in hits[0][0].page_content

    def test_vector_search_can_return_embeddings(self, store):
        store.add(DOCS, VECTORS)
        hits = store.search_vector(_vec(1), k=2, with_vectors=True)
        assert len(hits[0]) == 3
        assert len(hits[0][2]) == DIM

    def test_documents_for_files(self, store):
        store.add(DOCS, VECTORS)
        assert len(store.documents_for_files(["terms.pdf"])) == 2
        assert store.documents_for_files(["missing.pdf"]) == []

    def test_delete_source_clears_both_indexes(self, store):
        store.add(DOCS, VECTORS)
        assert store.delete_source("terms.pdf") == 2
        assert store.count() == 1
        assert store.search_keyword("corporate discount", k=3) == []

    def test_clear(self, store):
        store.add(DOCS, VECTORS)
        store.clear()
        assert store.count() == 0

    def test_persists_across_connections(self, tmp_path):
        path = tmp_path / "corpus.db"
        first = DocumentStore(path, dimension=DIM)
        first.add(DOCS, VECTORS)
        first.close()

        second = DocumentStore(path)
        assert second.count() == 3
        assert second.search_vector(_vec(1), k=1)
        second.close()

    def test_changing_embedding_size_resets_the_index(self, tmp_path):
        """Vectors from a different model are meaningless, so the corpus is cleared."""
        path = tmp_path / "corpus.db"
        first = DocumentStore(path, dimension=DIM)
        first.add(DOCS, VECTORS)
        first.close()

        second = DocumentStore(path, dimension=DIM * 2)
        assert second.count() == 0
        second.close()

    def test_mismatched_inputs_rejected(self, store):
        with pytest.raises(ValueError):
            store.add(DOCS, VECTORS[:1])


class TestFtsQuery:
    def test_tokens_are_quoted(self):
        assert _fts_query("corporate discount") == '"corporate" OR "discount"'

    def test_punctuation_dropped(self):
        assert _fts_query("What is AWS?") == '"what" OR "is" OR "aws"'

    def test_empty_query(self):
        assert _fts_query("?!") == ""


class TestHybridRetrieve:
    def test_finds_by_meaning_and_by_word(self, store):
        store.add(DOCS, VECTORS)
        embedder = _StubEmbedder({"corporate": 1, "delivery": 2, "bouquet": 3})
        results = hybrid_retrieve("corporate discount", store, top_k=2, embedder=embedder)
        assert results
        assert "12 percent" in results[0].page_content

    def test_works_without_an_embedder(self, store):
        """If embeddings are unavailable, keyword search alone must still answer."""
        store.add(DOCS, VECTORS)
        results = hybrid_retrieve("corporate discount", store, top_k=2, embedder=None)
        assert results
        assert "12 percent" in results[0].page_content

    def test_survives_a_broken_embedder(self, store):
        class Broken:
            def embed_query(self, text):
                raise RuntimeError("model not installed")

        store.add(DOCS, VECTORS)
        assert hybrid_retrieve("corporate discount", store, top_k=2, embedder=Broken())

    def test_empty_store(self, store):
        assert hybrid_retrieve("anything", store, top_k=3, embedder=None) == []


class TestFusionAndDiversity:
    def test_rrf_favours_agreement(self):
        a, b, c = Document("a"), Document("b"), Document("c")
        merged = _rrf_merge([[a, b], [b, c]])
        assert merged[0].page_content == "b"

    def test_mmr_avoids_near_duplicates(self):
        query = [1.0, 0.2, 0.0]
        candidates = [
            (Document("one"), [1.0, 0.0, 0.0]),
            (Document("two"), [0.98, 0.05, 0.0]),   # near-duplicate of "one"
            (Document("three"), [0.3, 1.0, 0.0]),   # different direction
        ]
        picked = _mmr(query, candidates, k=2, lambda_mult=0.5)
        assert "three" in {d.page_content for d in picked}
        assert not {"one", "two"} <= {d.page_content for d in picked}

    def test_mmr_on_empty(self):
        assert _mmr(_vec(1), [], k=3) == []


class TestCoreTerms:
    def test_strips_question_words(self):
        assert _core_terms("What is AWS?") == "aws"

    def test_returns_none_when_nothing_stripped(self):
        assert _core_terms("Amazon Web Services") is None

    def test_returns_none_for_long_queries(self):
        assert _core_terms("what are the key features of the corporate discount and the bulk tier") is None


class TestSplitText:
    def test_short_text_is_one_chunk(self):
        assert split_text("hello world", chunk_size=100, chunk_overlap=20) == ["hello world"]

    def test_long_text_is_split(self):
        text = "\n\n".join(f"Paragraph {i} with some content in it." for i in range(50))
        chunks = split_text(text, chunk_size=200, chunk_overlap=40)
        assert len(chunks) > 1
        assert all(len(c) <= 260 for c in chunks)

    def test_no_content_is_lost(self):
        text = "\n\n".join(f"Unique marker {i}." for i in range(30))
        joined = " ".join(split_text(text, chunk_size=120, chunk_overlap=20))
        for i in range(30):
            assert f"Unique marker {i}." in joined

    def test_empty_input(self):
        assert split_text("") == []

    def test_overlap_must_be_smaller_than_chunk(self):
        with pytest.raises(ValueError):
            split_text("some text", chunk_size=100, chunk_overlap=100)
