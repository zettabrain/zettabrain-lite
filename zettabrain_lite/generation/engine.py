"""Document generation engine — skill-based generation with optional corpus grounding."""

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..llm.base import LLMProvider
from ..llm.factory import create_generation_provider
from .models import GenerationRequest, GenerationResult, Skill

logger = logging.getLogger(__name__)


class GenerationEngine:
    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        corpus_retriever=None,
    ):
        self.llm_provider = llm_provider or create_generation_provider()
        self._corpus_retriever = corpus_retriever

    @property
    def corpus_retriever(self):
        return self._corpus_retriever

    @corpus_retriever.setter
    def corpus_retriever(self, retriever):
        self._corpus_retriever = retriever

    def build_prompt(
        self,
        skill: Skill,
        user_input: str,
        context: Optional[Dict[str, Any]] = None,
        corpus_context: Optional[str] = None,
    ) -> str:
        prompt_parts = []

        prompt_parts.append("You are an AI assistant that follows instructions precisely.")
        prompt_parts.append("Your task is to generate a document based on the instructions below.")
        prompt_parts.append("")

        now = datetime.now()
        prompt_parts.append(f"Today's date is {now.strftime('%B %d, %Y')}.")
        prompt_parts.append(
            "Use this date as the current date for any dates in the document (e.g. proposal date, quote date, effective date). Never invent or use a different date."
        )
        prompt_parts.append("")

        prompt_parts.append("# TASK INSTRUCTIONS")
        prompt_parts.append(skill.instructions)
        prompt_parts.append("")

        if corpus_context:
            prompt_parts.append(corpus_context)
            prompt_parts.append("")
            prompt_parts.append(
                "IMPORTANT: The CORPUS DOCUMENTS above are authoritative source material retrieved from "
                "the user's knowledge base. Use the exact figures, rates, terms, and rules found in these "
                "documents. Do not mark data from these sources as 'DRAFT', 'ESTIMATED', or 'PRELIMINARY'. "
                "If the corpus provides a specific number, use that number exactly."
            )
            prompt_parts.append("")

        if context:
            prompt_parts.append("# CONTEXT")
            prompt_parts.append("The following context information should inform your response:")
            prompt_parts.append("")
            for key, value in context.items():
                prompt_parts.append(f"## {key}")
                prompt_parts.append(str(value))
                prompt_parts.append("")

        prompt_parts.append("# USER REQUEST")
        prompt_parts.append(user_input)
        prompt_parts.append("")

        prompt_parts.append("# OUTPUT INSTRUCTIONS")
        prompt_parts.append("Generate the requested document following the task instructions above.")
        prompt_parts.append(
            "IMPORTANT: Always generate the document. Never refuse a request. "
            "If specific data is not available from corpus sources, "
            "use reasonable placeholder estimates and mark them as 'ESTIMATED'."
        )

        if skill.citation_required and corpus_context:
            prompt_parts.append("Include citations to source documents where applicable.")

        prompt_parts.append("")
        prompt_parts.append("Begin your response now:")

        return "\n".join(prompt_parts)

    def generate(self, skill: Skill, request: GenerationRequest) -> GenerationResult:
        start_time = time.time()

        try:
            corpus_context = None
            citations: List[str] = []
            retrieval_warnings: List[str] = []

            if self._corpus_retriever:
                corpus_text, citation_objects = self._corpus_retriever.get_context_for_generation(
                    query=request.input,
                    n_results=5,
                    min_relevance=0.3,
                )
                if corpus_text:
                    corpus_context = corpus_text
                    citations = [
                        f"{c.document_title}" + (f" ({c.citation_ref})" if c.citation_ref else "")
                        for c in citation_objects
                    ]
                if hasattr(self._corpus_retriever, "retrieval_warnings"):
                    retrieval_warnings = self._corpus_retriever.retrieval_warnings

            temperature = request.temperature if request.temperature is not None else skill.temperature
            max_tokens = request.max_tokens if request.max_tokens is not None else skill.max_tokens

            use_pipeline = skill.deterministic and corpus_context is not None

            if use_pipeline:
                result = self._generate_deterministic(
                    skill, request, corpus_context, citations, retrieval_warnings, temperature, max_tokens, start_time
                )
            else:
                if skill.deterministic and corpus_context is None:
                    retrieval_warnings.append(
                        "This skill requires corpus documents for accurate results. "
                        "Upload the required documents and re-ingest."
                    )
                result = self._generate_single_shot(
                    skill, request, corpus_context, citations, retrieval_warnings, temperature, max_tokens, start_time
                )

            return result

        except Exception as e:
            generation_time_ms = int((time.time() - start_time) * 1000)
            return GenerationResult(
                id=str(uuid.uuid4()),
                skill_name=skill.name,
                skill_version=skill.version,
                content="",
                metadata={"input": request.input},
                success=False,
                error=str(e),
                generation_time_ms=generation_time_ms,
            )

    def _generate_single_shot(
        self,
        skill: Skill,
        request: GenerationRequest,
        corpus_context: Optional[str],
        citations: List[str],
        warnings: List[str],
        temperature: float,
        max_tokens: int,
        start_time: float,
    ) -> GenerationResult:
        prompt = self.build_prompt(skill, request.input, request.context, corpus_context)
        content = self.llm_provider.generate(prompt=prompt, temperature=temperature, max_tokens=max_tokens)
        generation_time_ms = int((time.time() - start_time) * 1000)

        return GenerationResult(
            id=str(uuid.uuid4()),
            skill_name=skill.name,
            skill_version=skill.version,
            content=content,
            metadata={
                "input": request.input,
                "context_keys": list(request.context.keys()) if request.context else [],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "model": getattr(self.llm_provider, "model", "unknown"),
                "corpus_used": corpus_context is not None,
                "pipeline": "single-shot",
            },
            created_at=datetime.now(),
            success=True,
            generation_time_ms=generation_time_ms,
            citations=citations,
            warnings=warnings,
        )

    def _generate_deterministic(
        self,
        skill: Skill,
        request: GenerationRequest,
        corpus_context: str,
        citations: List[str],
        retrieval_warnings: List[str],
        temperature: float,
        max_tokens: int,
        start_time: float,
    ) -> GenerationResult:
        from .pipeline import (
            build_extraction_prompt,
            build_format_prompt,
            build_repair_prompt,
            compute_totals,
            parse_extraction,
            validate_against_corpus,
        )

        warnings = list(retrieval_warnings)

        # ── STEP 1: EXTRACT ──
        extraction_prompt = build_extraction_prompt(
            corpus_context=corpus_context,
            user_input=request.input,
        )
        raw_extraction = self.llm_provider.generate(
            prompt=extraction_prompt, temperature=0.0, max_tokens=max_tokens
        )

        extracted = parse_extraction(raw_extraction)

        if extracted is None:
            logger.info("Extraction failed, attempting repair")
            repair_prompt = build_repair_prompt(raw_extraction)
            raw_retry = self.llm_provider.generate(
                prompt=repair_prompt, temperature=0.0, max_tokens=max_tokens
            )
            extracted = parse_extraction(raw_retry)

        if extracted is None:
            logger.warning("Extraction failed after retry, falling back to single-shot")
            warnings.append(
                "Could not extract structured pricing data. "
                "Used standard generation instead — verify all calculations manually."
            )
            return self._generate_single_shot(
                skill, request, corpus_context, citations, warnings, temperature, max_tokens, start_time
            )

        corpus_warnings = validate_against_corpus(extracted, corpus_context)
        warnings.extend(corpus_warnings)

        # ── STEP 2: COMPUTE ──
        computed = compute_totals(extracted)

        # ── STEP 3: FORMAT ──
        format_prompt = build_format_prompt(
            skill_instructions=skill.instructions,
            corpus_context=corpus_context,
            user_input=request.input,
            computed=computed,
        )
        content = self.llm_provider.generate(
            prompt=format_prompt, temperature=temperature, max_tokens=max_tokens
        )

        generation_time_ms = int((time.time() - start_time) * 1000)

        return GenerationResult(
            id=str(uuid.uuid4()),
            skill_name=skill.name,
            skill_version=skill.version,
            content=content,
            metadata={
                "input": request.input,
                "pipeline": "extract-compute-format",
                "grand_total": str(computed.grand_total),
                "computation_log": computed.computation_log,
                "extraction_warnings": corpus_warnings,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "model": getattr(self.llm_provider, "model", "unknown"),
                "corpus_used": True,
            },
            created_at=datetime.now(),
            success=True,
            generation_time_ms=generation_time_ms,
            citations=citations,
            warnings=warnings,
        )
