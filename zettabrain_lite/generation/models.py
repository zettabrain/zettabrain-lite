"""Pydantic models for the skill-based generation engine."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Skill(BaseModel):
    name: str
    version: str
    description: str
    business_type: str = "generic"
    author: Optional[str] = None

    requires_corpus: bool = False
    requires_discovery: List[str] = Field(default_factory=list)

    inputs: List[str] | Dict[str, Any] = Field(default_factory=list)
    outputs: List[str] | Dict[str, Any] = Field(default_factory=list)
    references: Dict[str, str] = Field(default_factory=dict)

    instructions: str

    temperature: float = 0.7
    max_tokens: int = 2000
    citation_required: bool = False
    escalation_triggers: List[str] = Field(default_factory=list)

    skill_type: str = "document"
    corpus_doc_types: List[str] = Field(default_factory=list)

    tags: List[str] = Field(default_factory=list)
    deprecated: bool = False


class GenerationRequest(BaseModel):
    input: str
    skill_name: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class GenerationResult(BaseModel):
    id: str
    skill_name: str
    skill_version: str
    content: str
    metadata: Dict[str, Any]
    created_at: datetime = Field(default_factory=datetime.now)
    success: bool = True
    error: Optional[str] = None
    citations: List[str] = Field(default_factory=list)
    generation_time_ms: Optional[int] = None
