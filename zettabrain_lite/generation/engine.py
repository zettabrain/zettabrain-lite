"""Document generation engine — skill-based generation with optional corpus grounding."""

import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..llm.base import LLMProvider
from ..llm.factory import create_generation_provider
from .models import GenerationRequest, GenerationResult, Skill


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

            if skill.requires_corpus and self._corpus_retriever:
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

            prompt = self.build_prompt(skill, request.input, request.context, corpus_context)

            temperature = request.temperature if request.temperature is not None else skill.temperature
            max_tokens = request.max_tokens if request.max_tokens is not None else skill.max_tokens

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
                },
                created_at=datetime.now(),
                success=True,
                generation_time_ms=generation_time_ms,
                citations=citations,
            )

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
