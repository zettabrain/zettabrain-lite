"""File loaders for the document types the corpus accepts.

Replaces langchain_community.document_loaders, which pulls the wider LangChain tree in for
what is a thin wrapper over pypdf, docx2txt and open().
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from .documents import Document

log = logging.getLogger(__name__)


def load_text(path: str) -> list[Document]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [Document(page_content=text, metadata={"source": path, "page": 0})] if text.strip() else []


def load_pdf(path: str) -> list[Document]:
    """One document per page. PyMuPDF first for layout, pypdf as the fallback."""
    try:
        import pymupdf  # noqa: PLC0415

        docs = []
        with pymupdf.open(path) as pdf:
            for page_number, page in enumerate(pdf):
                text = page.get_text("text").strip()
                if text:
                    docs.append(Document(page_content=text, metadata={"source": path, "page": page_number}))
        if docs:
            return docs
    except ImportError:
        pass
    except Exception:
        log.debug("PyMuPDF could not read %s", path, exc_info=True)

    try:
        from pypdf import PdfReader  # noqa: PLC0415

        reader = PdfReader(path)
        docs = []
        for page_number, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            if text:
                docs.append(Document(page_content=text, metadata={"source": path, "page": page_number}))
        return docs
    except Exception as exc:
        log.warning("Could not read PDF %s: %s", Path(path).name, exc)
        return []


def load_docx(path: str) -> list[Document]:
    try:
        import docx2txt  # noqa: PLC0415

        text = (docx2txt.process(path) or "").strip()
        return [Document(page_content=text, metadata={"source": path, "page": 0})] if text else []
    except Exception as exc:
        log.warning("Could not read Word file %s: %s", Path(path).name, exc)
        return []


def load_csv(path: str) -> list[Document]:
    """One document per row, rendered as key=value so a chunk carries its column names."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except Exception as exc:
        log.warning("Could not read CSV %s: %s", Path(path).name, exc)
        return []
    if len(rows) < 2:
        return []
    headers = [h.strip() for h in rows[0]]
    docs = []
    for i, row in enumerate(rows[1:]):
        pairs = [f"{h}: {v}" for h, v in zip(headers, row) if str(v).strip()]
        if pairs:
            docs.append(Document(page_content=" | ".join(pairs), metadata={"source": path, "page": i}))
    return docs


def load_xlsx(path: str) -> list[Document]:
    """One document per row across every sheet."""
    try:
        import openpyxl  # noqa: PLC0415

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        log.warning("Could not read spreadsheet %s: %s", Path(path).name, exc)
        return []
    docs = []
    try:
        for sheet in workbook.worksheets:
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                line = " | ".join(str(v) for v in row if v is not None)
                if line.strip():
                    docs.append(
                        Document(
                            page_content=line,
                            metadata={"source": path, "page": i, "sheet": sheet.title},
                        )
                    )
    finally:
        workbook.close()
    return docs


_LOADERS = {
    ".pdf": load_pdf,
    ".txt": load_text,
    ".md": load_text,
    ".docx": load_docx,
    ".doc": load_docx,
    ".csv": load_csv,
    ".xlsx": load_xlsx,
    ".xls": load_xlsx,
}


def load_file(path: str) -> list[Document]:
    """Load any supported file. Returns [] for unsupported or unreadable files."""
    loader = _LOADERS.get(Path(path).suffix.lower())
    if not loader:
        return []
    return loader(path)
