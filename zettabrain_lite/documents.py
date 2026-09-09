"""The document type passed between ingestion, storage and retrieval.

Replaces langchain_core.documents.Document. The field names are kept identical so callers
and templates that read `.page_content` and `.metadata` need no change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Document:
    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.page_content, tuple(sorted(self.metadata.items(), key=str))))
