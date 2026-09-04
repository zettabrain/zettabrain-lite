"""Integration tests for the GenerationEngine with the ECF pipeline."""

from typing import Any, Dict, Iterator

from zettabrain_lite.generation.engine import GenerationEngine
from zettabrain_lite.generation.models import GenerationRequest, Skill
from zettabrain_lite.llm.base import LLMProvider


class MockLLMProvider(LLMProvider):
    """LLM provider that returns canned responses in sequence."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call_index = 0

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> str:
        if self._call_index < len(self._responses):
            result = self._responses[self._call_index]
            self._call_index += 1
            return result
        return ""

    def stream(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> Iterator[str]:
        yield self.generate(prompt, temperature, max_tokens)

    def check_health(self) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"provider": "mock", "model": "test"}


class MockCorpusRetriever:
    def __init__(self, context: str):
        self._context = context
        self.retrieval_warnings: list[str] = []

    def get_context_for_generation(self, query, n_results=5, min_relevance=0.3, **kwargs):
        from dataclasses import dataclass

        @dataclass
        class Citation:
            document_title: str
            citation_ref: str = ""

        return self._context, [Citation(document_title="test-doc", citation_ref="[1]")]


VALID_EXTRACTION_JSON = """{
  "line_items": [
    {
      "description": "R-22 Refrigerant - Reclaimed",
      "unit": "lb",
      "quantity": 100,
      "unit_price": 65.00,
      "discount_percent": 5,
      "discount_reason": "100-249 lbs volume discount",
      "source_ref": "pricing-rules"
    }
  ],
  "fees": [
    {"description": "Zone 2 Delivery", "amount": 45, "source_ref": "pricing-rules"},
    {"description": "Emergency Delivery", "amount": 150, "source_ref": "pricing-rules"}
  ],
  "taxes": [
    {"description": "Virginia Sales Tax", "rate_percent": 5.3, "source_ref": "pricing-rules"}
  ],
  "customer": {"name": "Test Corp", "contact": "Jane Doe"},
  "metadata": {"delivery_speed": "emergency"}
}"""

CORPUS_TEXT = """# Pricing Rules
R-22 Reclaimed: $65.00/lb
Volume: 100-249 lbs = 5% off
Zone 2: $45
Emergency: +$150
Tax: 5.3%"""

FORMATTED_DOCUMENT = """QUOTE
Customer: Test Corp
R-22 Refrigerant: 100 lbs @ $65.00 = $6,500.00
Discount (5%): -$325.00
Delivery: $195.00
Tax (5.3%): $337.88
GRAND TOTAL: $6,712.88"""


def _make_skill(deterministic: bool = True, requires_corpus: bool = True) -> Skill:
    return Skill(
        name="test-quote",
        version="1.0.0",
        description="Test quote skill",
        instructions="Generate a professional quote with line items, fees, taxes, and a grand total.",
        skill_type="quote",
        deterministic=deterministic,
        requires_corpus=requires_corpus,
        temperature=0.3,
        max_tokens=2000,
    )


class TestDeterministicPipeline:
    def test_happy_path(self):
        llm = MockLLMProvider([VALID_EXTRACTION_JSON, FORMATTED_DOCUMENT])
        retriever = MockCorpusRetriever(CORPUS_TEXT)
        engine = GenerationEngine(llm_provider=llm, corpus_retriever=retriever)

        result = engine.generate(_make_skill(), GenerationRequest(input="Need 100 lbs R-22"))

        assert result.success
        assert result.metadata["pipeline"] == "extract-compute-format"
        assert result.metadata["grand_total"] == "6712.88"
        assert "GRAND TOTAL" in result.content

    def test_extraction_fails_then_repairs(self):
        llm = MockLLMProvider(["not valid json", VALID_EXTRACTION_JSON, FORMATTED_DOCUMENT])
        retriever = MockCorpusRetriever(CORPUS_TEXT)
        engine = GenerationEngine(llm_provider=llm, corpus_retriever=retriever)

        result = engine.generate(_make_skill(), GenerationRequest(input="Need 100 lbs R-22"))

        assert result.success
        assert result.metadata["pipeline"] == "extract-compute-format"

    def test_extraction_fails_completely_falls_back(self):
        llm = MockLLMProvider(["garbage", "also garbage", "Fallback single-shot document"])
        retriever = MockCorpusRetriever(CORPUS_TEXT)
        engine = GenerationEngine(llm_provider=llm, corpus_retriever=retriever)

        result = engine.generate(_make_skill(), GenerationRequest(input="Need a quote"))

        assert result.success
        assert result.metadata["pipeline"] == "single-shot"
        assert len(result.warnings) > 0
        assert any("verify" in w.lower() for w in result.warnings)

    def test_non_deterministic_skill_uses_single_shot(self):
        llm = MockLLMProvider(["Single-shot output"])
        retriever = MockCorpusRetriever(CORPUS_TEXT)
        engine = GenerationEngine(llm_provider=llm, corpus_retriever=retriever)

        result = engine.generate(_make_skill(deterministic=False), GenerationRequest(input="Need a summary"))

        assert result.success
        assert result.metadata["pipeline"] == "single-shot"

    def test_deterministic_without_corpus_falls_back(self):
        llm = MockLLMProvider(["Single-shot output"])
        engine = GenerationEngine(llm_provider=llm, corpus_retriever=None)

        result = engine.generate(_make_skill(), GenerationRequest(input="Need a quote"))

        assert result.success
        assert result.metadata["pipeline"] == "single-shot"
        assert any("corpus" in w.lower() for w in result.warnings)

    def test_corpus_warnings_propagate(self):
        llm = MockLLMProvider([VALID_EXTRACTION_JSON, FORMATTED_DOCUMENT])
        retriever = MockCorpusRetriever(CORPUS_TEXT)
        retriever.retrieval_warnings = ["Document 'missing.md' not found in corpus."]
        engine = GenerationEngine(llm_provider=llm, corpus_retriever=retriever)

        result = engine.generate(_make_skill(), GenerationRequest(input="Need a quote"))

        assert result.success
        assert any("missing.md" in w for w in result.warnings)
