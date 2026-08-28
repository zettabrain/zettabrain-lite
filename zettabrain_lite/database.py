"""ZettaBrain Lite — SQLite database for chat and generation history."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

from .config import DATABASE_PATH, DATABASE_URL


class ChatHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    question: str
    answer: str
    model: str = ""
    confidence: float = 0.0
    chunks_searched: int = 0
    duration_ms: int = 0
    sources: str = "[]"
    created_at: datetime = Field(default_factory=datetime.now)


class GenerationHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    skill_name: str
    skill_version: str = ""
    input_text: str
    output_content: str
    citations: Optional[str] = None
    generation_time_ms: Optional[int] = None
    metadata_json: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(DATABASE_URL, echo=False)
        SQLModel.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    return Session(get_engine())
