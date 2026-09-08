"""
ZettaBrain — Document Ingestion Utility

Incrementally ingests documents into the ChromaDB vector store.
Skips already-ingested files using MD5 hash tracking.

Usage:
    python 05_ingest_documents.py                        # ingest /mnt/Rag-data (default)
    python 05_ingest_documents.py --folder /mnt/Rag-data/contracts
    python 05_ingest_documents.py --file /mnt/Rag-data/report.pdf
    python 05_ingest_documents.py --stats                # show what's ingested
    python 05_ingest_documents.py --clear                # wipe the vector store
    python 05_ingest_documents.py --rebuild              # force re-ingest all files
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from langchain_chroma import Chroma
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from zettabrain_lite.price_list import currency_from_header, detect_columns, parse_price
    from zettabrain_lite.price_list import upsert_items as _upsert_price_items

    _HAS_PRICE_LIST = True
except ImportError:
    _HAS_PRICE_LIST = False

try:
    from zettabrain_lite.retrieval import rebuild_bm25_index

    _HAS_RETRIEVAL = True
except ImportError:
    try:
        from zettabrain_rag.retrieval import rebuild_bm25_index

        _HAS_RETRIEVAL = True
    except ImportError:
        _HAS_RETRIEVAL = False

try:
    from zettabrain_lite.storage_profile import get_chunk_profile as _get_chunk_profile

    _HAS_STORAGE_PROFILE = True
except ImportError:
    try:
        from zettabrain_rag.storage_profile import get_chunk_profile as _get_chunk_profile

        _HAS_STORAGE_PROFILE = True
    except ImportError:
        _HAS_STORAGE_PROFILE = False


# -------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------
def _load_zettabrain_env() -> dict:
    cfg = {}
    candidates = [
        "/opt/zettabrain/src/zettabrain.env",
        "/zettabrain/src/zettabrain.env",
    ]
    # Windows: %LOCALAPPDATA%\ZettaBrain\zettabrain.env
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(os.path.join(local_app, "ZettaBrain", "zettabrain.env"))
    for p in candidates:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip().strip('"').strip("'")
            break
    return cfg


_cfg = _load_zettabrain_env()


def _get(key, fallback):
    return os.environ.get(key) or _cfg.get(key) or fallback


def _default_chroma() -> str:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
        return os.path.join(local_app, "ZettaBrain", "zettabrain_vectorstore")
    return "/opt/zettabrain/src/zettabrain_vectorstore"


def _default_docs() -> str:
    if os.name == "nt":
        return os.path.join(os.path.expanduser("~"), "Documents")
    return "/opt/zettabrain/data"


def _default_hash_cache() -> str:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
        return os.path.join(local_app, "ZettaBrain", "ingested_files.json")
    return "./ingested_files.json"


DOCS_FOLDER = _get("ZETTABRAIN_DOCS", _get("RAG_DATA_PATH", _default_docs()))
CHROMA_PATH = _get("ZETTABRAIN_CHROMA", _default_chroma())
EMBED_MODEL = os.environ.get("ZETTABRAIN_EMBED_MODEL", "nomic-embed-text")
HASH_CACHE = _default_hash_cache()
INGEST_ERROR_LOG = str(Path(CHROMA_PATH).parent / "ingest_errors.log")

try:
    from zettabrain_lite.config import SUPPORTED_EXTENSIONS as SUPPORTED
except ImportError:  # running the script standalone, outside the installed package
    SUPPORTED = {".pdf", ".txt", ".md", ".docx", ".doc", ".xlsx", ".xls", ".csv"}


# -------------------------------------------------------
# HELPERS
# -------------------------------------------------------
def get_file_hash(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def load_hash_cache() -> dict:
    if os.path.exists(HASH_CACHE):
        with open(HASH_CACHE) as f:
            return json.load(f)
    return {}


def save_hash_cache(cache: dict):
    with open(HASH_CACHE, "w") as f:
        json.dump(cache, f, indent=2)


def log_ingest_error(filepath: str, reason: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] FAIL  {Path(filepath).name}  —  {reason}\n"
    try:
        with open(INGEST_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _load_pdf(filepath: str):
    """Try PyMuPDF first (better layout handling), fall back to pypdf."""
    try:
        import fitz  # pymupdf

        docs = []
        pdf = fitz.open(filepath)
        for page_num, page in enumerate(pdf):
            text = page.get_text("text").strip()
            if text:
                from langchain_core.documents import Document

                docs.append(Document(page_content=text, metadata={"source": filepath, "page": page_num}))
        pdf.close()
        if docs:
            return docs
        # PyMuPDF found no text — fall through to pypdf
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: pypdf
    try:
        return PyPDFLoader(filepath).load()
    except Exception as e:
        print(f"  [WARN] Could not parse PDF {Path(filepath).name}: {e}")
        return []


# ── Price list detection helpers ──────────────────────────────────────────────
_PRICE_LIST_FILENAME_PATTERNS = re.compile(
    r"(price[_\- ]?list|price[_\- ]?sheet|rate[_\- ]?card|catalog(?:ue)?|fee[_\- ]?schedule|tariff|cost[_\- ]?sheet)",
    re.IGNORECASE,
)


def _is_price_list_file(filepath: str) -> bool:
    return bool(_PRICE_LIST_FILENAME_PATTERNS.search(Path(filepath).name))


def _rows_from_headers_and_data(headers: list, data_rows: list) -> list[dict]:
    """Convert raw table headers + row values into price_list_items dicts."""
    header_strs = [str(h) if h is not None else "" for h in headers]
    col_map = detect_columns(header_strs)
    if not col_map:
        return []

    # Spreadsheets hold bare numbers and name the currency in the header instead.
    header_currency = currency_from_header(header_strs[col_map["price"]])

    rows: list[dict] = []
    for raw in data_rows:
        if len(raw) <= max(col_map.values()):
            continue
        name_val = raw[col_map["name"]]
        price_val = raw[col_map["price"]]
        if name_val is None or str(name_val).strip() == "":
            continue
        price, currency = parse_price(price_val)
        if price is None:
            continue
        row: dict = {
            "name": str(name_val).strip(),
            "base_price": price,
            "currency": currency or header_currency,
        }
        for field, idx in col_map.items():
            if field in ("name", "price", "currency") or idx >= len(raw):
                continue
            val = raw[idx]
            row[field] = str(val).strip() if val is not None else ""
        rows.append(row)
    return rows


_HEADER_SCAN_ROWS = 20


def _find_header_row(values: list) -> tuple[int, dict]:
    """Locate the header row in a sheet or table.

    Real-world price lists put a title block above the header, so the header is rarely row 1.
    Scans the first _HEADER_SCAN_ROWS rows and returns the (index, column_map) of the row that
    detect_columns resolves into the most fields. Returns (-1, {}) when no row qualifies.
    """
    best_idx, best_map = -1, {}
    for i, row in enumerate(values[:_HEADER_SCAN_ROWS]):
        if row is None:
            continue
        col_map = detect_columns([str(h) if h is not None else "" for h in row])
        if len(col_map) > len(best_map):
            best_idx, best_map = i, col_map
    return best_idx, best_map


def _price_column_is_formulas(sheet, header_idx: int, price_col: int) -> bool:
    """True when the detected price column holds mostly formulas rather than literal values.

    Distinguishes a real price list (literal prices) from a derived sheet such as a quote
    builder or summary tab, whose price cells are INDEX/MATCH lookups. Uses no sheet-name
    heuristics, so it stays industry- and language-agnostic.
    """
    formulas = literals = 0
    for row in sheet.iter_rows(min_row=header_idx + 2, max_row=header_idx + 41):
        if price_col >= len(row):
            continue
        val = row[price_col].value
        if val is None:
            continue
        if isinstance(val, str) and val.startswith("="):
            formulas += 1
        else:
            literals += 1
    return formulas > literals


def _load_xlsx_price_rows(filepath: str) -> list[dict]:
    """Extract price list rows from an XLSX file, across all sheets."""
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError:
        print(f"  [SKIP] {Path(filepath).name} — openpyxl not installed (pip install openpyxl)")
        return []
    try:
        wb_values = openpyxl.load_workbook(filepath, data_only=True)
        wb_formulas = openpyxl.load_workbook(filepath, data_only=False)
    except Exception as e:
        print(f"  [WARN] {Path(filepath).name} — XLSX read error: {e}")
        return []

    try:
        candidates: list[dict] = []
        for sheet in wb_values.worksheets:
            values = list(sheet.iter_rows(values_only=True))
            if len(values) < 2:
                continue
            header_idx, col_map = _find_header_row(values)
            if not col_map:
                continue
            rows = _rows_from_headers_and_data(list(values[header_idx]), values[header_idx + 1 :])
            if not rows:
                continue
            derived = False
            try:
                derived = _price_column_is_formulas(
                    wb_formulas[sheet.title], header_idx, col_map["price"]
                )
            except Exception:
                pass  # If we cannot tell, keep the sheet rather than lose data.
            candidates.append({"title": sheet.title, "rows": rows, "derived": derived})

        # Drop calculated sheets only when at least one sheet holds literal prices, so a
        # workbook whose only price column is computed still ingests.
        if any(not c["derived"] for c in candidates):
            for c in candidates:
                if c["derived"]:
                    print(
                        f"  [PL] {Path(filepath).name} — skipped sheet '{c['title']}' "
                        f"({len(c['rows'])} row(s)): prices are formulas, not a source price list"
                    )
            candidates = [c for c in candidates if not c["derived"]]

        all_rows: list[dict] = []
        for c in candidates:
            print(f"  [PL] {Path(filepath).name} — sheet '{c['title']}': {len(c['rows'])} price item(s)")
            all_rows.extend(c["rows"])
        return all_rows
    finally:
        wb_values.close()
        wb_formulas.close()


def _load_csv_price_rows(filepath: str) -> list[dict]:
    """Extract price list rows from a CSV file."""
    import csv  # noqa: PLC0415

    try:
        with open(filepath, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
        if len(rows) < 2:
            return []
        header_idx, col_map = _find_header_row(rows)
        if not col_map:
            return []
        return _rows_from_headers_and_data(rows[header_idx], rows[header_idx + 1 :])
    except Exception as e:
        print(f"  [WARN] {Path(filepath).name} — CSV read error: {e}")
        return []


def _load_pdf_price_rows(filepath: str) -> list[dict]:
    """Extract price list rows from PDF tables using PyMuPDF find_tables()."""
    try:
        import fitz  # noqa: PLC0415
    except ImportError:
        print(
            f"  [WARN] {Path(filepath).name} — PDF table extraction requires PyMuPDF.\n"
            "         Convert the price list to XLSX or CSV for reliable extraction,\n"
            "         or install PyMuPDF:  pip install pymupdf"
        )
        return []
    try:
        doc = fitz.open(filepath)
        all_rows: list[dict] = []
        for pno, page in enumerate(doc, 1):
            for tab in page.find_tables().tables:
                # extract() returns plain lists — no pandas dependency.
                table = tab.extract()
                if not table or len(table) < 2 or len(table[0]) < 2:
                    continue
                header_idx, col_map = _find_header_row(table)
                if not col_map:
                    continue
                rows = _rows_from_headers_and_data(table[header_idx], table[header_idx + 1 :])
                if rows:
                    print(f"  [PL] {Path(filepath).name} — page {pno}: {len(rows)} price item(s)")
                    all_rows.extend(rows)
        doc.close()
        return all_rows
    except Exception as e:
        print(f"  [WARN] {Path(filepath).name} — PDF table extraction error: {e}")
        return []


# ── Rates and thresholds (VAT, discounts) stated as label/value pairs ─────────
# Order matters: a label such as "Bulk order discount (orders above threshold)" names both a
# discount and a threshold. The rate keywords are checked first so the qualifier does not win.
_SETTING_KINDS: list[tuple[str, re.Pattern]] = [
    ("tax", re.compile(r"\b(vat|gst|hst|sales tax|tax)\b", re.IGNORECASE)),
    ("discount", re.compile(r"\b(discount|rebate)\b", re.IGNORECASE)),
    ("surcharge", re.compile(r"\b(surcharge|markup|uplift|premium)\b", re.IGNORECASE)),
    ("threshold", re.compile(r"\b(threshold|minimum order|min order|order value)\b", re.IGNORECASE)),
]


def _classify_setting(label: str) -> str:
    for kind, pattern in _SETTING_KINDS:
        if pattern.search(label):
            return kind
    return ""


def _setting_from_pair(label: str, value: object) -> Optional[dict]:
    """Build a settings dict from a label/value pair, or None when it is not a rate."""
    label = str(label).strip()
    if not label or len(label) > 120:
        return None
    kind = _classify_setting(label)
    if not kind:
        return None

    raw = str(value).strip()
    if isinstance(value, str) and value.startswith("="):
        return None  # formula with no cached value
    percent_written = isinstance(value, str) and "%" in value
    amount, _currency = parse_price(value)
    if amount is None:
        return None

    # Thresholds are absolute money; everything else is a rate. Spreadsheets store rates
    # either as a fraction (0.075) or as a written percentage ("7.5%").
    if kind == "threshold":
        return {"label": label, "kind": kind, "percent": None, "amount": amount, "raw": raw}
    percent = amount * 100 if (amount <= 1 and not percent_written) else amount
    if percent > 100:
        return None
    return {"label": label, "kind": kind, "percent": round(percent, 4), "amount": None, "raw": raw}


def _load_xlsx_settings(filepath: str) -> list[dict]:
    """Extract rate/threshold settings stated as adjacent label/value cells.

    Handles the common pattern of an assumptions or inputs tab holding 'VAT rate | 0.075'.
    Language- and industry-agnostic: driven by the label text, not by sheet or file names.
    """
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError:
        return []
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    try:
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                cells = [c for c in row if c is not None]
                if len(cells) < 2:
                    continue
                setting = _setting_from_pair(cells[0], cells[1])
                if setting and setting["label"].lower() not in seen:
                    seen.add(setting["label"].lower())
                    found.append(setting)
    finally:
        wb.close()
    return found


def ingest_price_settings(filepath: str, settings: list[dict]) -> int:
    """Store rate/threshold settings for a price list file. Returns count stored."""
    if not _HAS_PRICE_LIST or not settings:
        return 0
    source_file = Path(filepath).name
    try:
        from zettabrain_lite.price_list import upsert_settings  # noqa: PLC0415

        count = upsert_settings(source_file, settings)
        for s in settings:
            shown = f"{s['percent']}%" if s.get("percent") is not None else s.get("raw", "")
            print(f"  [PL]   {source_file} — {s['kind']}: {s['label']} = {shown}")
        return count
    except Exception as e:
        print(f"  [WARN] {source_file} — settings DB error: {e}")
        return 0


def _tabular_to_text_chunks(rows: list[dict]) -> list[str]:
    """Convert price row dicts to searchable text lines for ChromaDB."""
    lines = []
    for r in rows:
        parts = []
        if r.get("sku"):
            parts.append(f"SKU: {r['sku']}")
        parts.append(f"Name: {r['name']}")
        if r.get("description"):
            parts.append(f"Description: {r['description']}")
        price_str = f"{r.get('currency', '')} {r['base_price']:.2f}".strip()
        parts.append(f"Price: {price_str}")
        if r.get("unit"):
            parts.append(f"Unit: {r['unit']}")
        if r.get("category"):
            parts.append(f"Category: {r['category']}")
        if r.get("notes"):
            parts.append(f"Notes: {r['notes']}")
        lines.append(" | ".join(parts))
    return lines


def ingest_price_list(filepath: str, price_rows: list[dict]) -> int:
    """Upsert price rows into the SQLite price list DB. Returns count inserted."""
    if not _HAS_PRICE_LIST or not price_rows:
        return 0
    source_file = Path(filepath).name
    try:
        count = _upsert_price_items(source_file, price_rows)
        print(f"  [PL]   {source_file} — {count} price items stored in DB")
        return count
    except Exception as e:
        print(f"  [WARN] {source_file} — price list DB error: {e}")
        return 0


def load_file(filepath: str):
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        return _load_pdf(filepath)
    elif ext in {".txt", ".md"}:
        return TextLoader(filepath, encoding="utf-8").load()
    elif ext in {".docx", ".doc"}:
        return Docx2txtLoader(filepath).load()
    elif ext in {".xlsx", ".xls"}:
        return _load_xlsx_as_documents(filepath)
    elif ext == ".csv":
        return _load_csv_as_documents(filepath)
    return []


def _load_xlsx_as_documents(filepath: str):
    """Return LangChain Documents from XLSX rows (one doc per row as key=value text)."""
    from langchain_core.documents import Document  # noqa: PLC0415

    price_rows = _load_xlsx_price_rows(filepath)
    if price_rows:
        lines = _tabular_to_text_chunks(price_rows)
        return [
            Document(page_content=line, metadata={"source": filepath, "page": i, "row_type": "price_list"})
            for i, line in enumerate(lines)
        ]
    # Fallback: raw cell dump if column detection fails
    try:
        import openpyxl  # noqa: PLC0415

        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        docs = []
        for sheet in wb.worksheets:
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                line = " | ".join(str(v) for v in row if v is not None)
                if line.strip():
                    docs.append(Document(page_content=line, metadata={"source": filepath, "page": i}))
        wb.close()
        return docs
    except Exception:
        return []


def _load_csv_as_documents(filepath: str):
    """Return LangChain Documents from CSV rows (one doc per row as key=value text)."""
    from langchain_core.documents import Document  # noqa: PLC0415

    price_rows = _load_csv_price_rows(filepath)
    if price_rows:
        lines = _tabular_to_text_chunks(price_rows)
        return [
            Document(page_content=line, metadata={"source": filepath, "page": i, "row_type": "price_list"})
            for i, line in enumerate(lines)
        ]
    import csv  # noqa: PLC0415

    try:
        docs = []
        with open(filepath, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            for i, row in enumerate(reader):
                line = " | ".join(cell for cell in row if cell.strip())
                if line:
                    docs.append(Document(page_content=line, metadata={"source": filepath, "page": i}))
        return docs
    except Exception:
        return []


BATCH_SIZE = 50  # chunks per embedding call

_DEFAULT_PROFILE = {"batch_size": 20, "storage_type": "local"}


def _get_storage_profile(docs_path: str) -> dict:
    if _HAS_STORAGE_PROFILE:
        try:
            return _get_chunk_profile(docs_path)
        except Exception:
            pass
    return dict(_DEFAULT_PROFILE)


def _adaptive_splitter(filepath: str, docs) -> RecursiveCharacterTextSplitter:
    """Tune chunk size by file type and text density."""
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        size, overlap = 1000, 150
    elif ext in {".docx", ".doc"}:
        size, overlap = 1200, 200
    else:  # .txt, .md
        size, overlap = 800, 100

    # Dense technical text (long sentences) → scale up
    sample = " ".join(d.page_content for d in docs[:5])
    sentences = [s.strip() for s in sample.replace("\n", " ").split(".") if s.strip()]
    if sentences and sum(len(s) for s in sentences) / len(sentences) > 120:
        size = int(size * 1.5)
        overlap = int(overlap * 1.5)

    return RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap, separators=["\n\n\n", "\n\n", "\n", ". ", " ", ""]
    )


def ingest_file(filepath: str, vectorstore, hash_cache: dict, profile: dict | None = None) -> bool:
    """Ingest a single file. Returns True if ingested, False if skipped."""
    filepath = str(Path(filepath).resolve())
    file_hash = get_file_hash(filepath)

    if hash_cache.get(filepath) == file_hash:
        print(f"  [SKIP] {Path(filepath).name} (already ingested)")
        return False

    try:
        docs = load_file(filepath)
    except Exception as e:
        reason = str(e)
        print(f"  [FAIL] {Path(filepath).name} — load error: {reason}")
        log_ingest_error(filepath, f"load error: {reason}")
        return False
    if not docs:
        ext = Path(filepath).suffix.lower()
        reason = "no text found (likely a scanned/image-only PDF)" if ext == ".pdf" else "unsupported or empty"
        print(f"  [SKIP] {Path(filepath).name} ({reason})")
        log_ingest_error(filepath, reason)
        return False

    splitter = _adaptive_splitter(filepath, docs)
    chunks = splitter.split_documents(docs)

    # Drop empty chunks that would cause ChromaDB to reject the batch
    chunks = [c for c in chunks if c.page_content.strip()]

    storage_type = (profile or _DEFAULT_PROFILE)["storage_type"]
    for chunk in chunks:
        chunk.metadata["source"] = filepath
        chunk.metadata["filename"] = Path(filepath).name
        chunk.metadata["storage_type"] = storage_type

    # Embed in small batches with retry so one Ollama hiccup doesn't abort the file
    added = 0
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        for attempt in range(3):
            try:
                vectorstore.add_documents(batch)
                added += len(batch)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  [WARN] {Path(filepath).name} batch {i // BATCH_SIZE + 1} failed: {e}")
                else:
                    time.sleep(2**attempt)

    if added == 0:
        reason = "no chunks embedded after splitting (document may be corrupt or too short)"
        print(f"  [FAIL] {Path(filepath).name} — {reason}")
        log_ingest_error(filepath, reason)
        return False

    hash_cache[filepath] = file_hash
    print(f"  [OK]   {Path(filepath).name} ({added}/{len(chunks)} chunks)")

    # Price list ingestion — runs for XLSX/CSV always, PDF only if filename matches
    ext = Path(filepath).suffix.lower()
    price_rows: list[dict] = []
    settings: list[dict] = []
    if ext in {".xlsx", ".xls"}:
        price_rows = _load_xlsx_price_rows(filepath)
        settings = _load_xlsx_settings(filepath)
    elif ext == ".csv":
        price_rows = _load_csv_price_rows(filepath)
    elif ext == ".pdf" and _is_price_list_file(filepath):
        price_rows = _load_pdf_price_rows(filepath)

    if price_rows:
        ingest_price_list(filepath, price_rows)
        if settings:
            ingest_price_settings(filepath, settings)
    elif _is_price_list_file(filepath) or ext in {".xlsx", ".xls", ".csv"}:
        # A file that looks like a price list but yielded nothing must say so. A silent zero
        # is indistinguishable from a healthy ingest, and leaves quotes with no prices to use.
        name = Path(filepath).name
        print(
            f"  [PL]   {name} — no price items found.\n"
            "         The text was indexed for search, but quotes cannot use it for pricing.\n"
            "         Check the sheet has a header row with a product column and a price column."
        )
        log_ingest_error(filepath, "no price items detected (indexed for search only)")

    return True


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ZettaBrain Document Ingestion")
    parser.add_argument(
        "--folder", default=DOCS_FOLDER, help=f"Ingest all supported files in a folder (default: {DOCS_FOLDER})"
    )
    parser.add_argument("--file", default=None, help="Ingest a single file")
    parser.add_argument("--clear", action="store_true", help="Clear the entire vector store")
    parser.add_argument("--stats", action="store_true", help="Show vector store statistics")
    parser.add_argument("--rebuild", action="store_true", help="Force re-ingestion of all files (clears hash cache)")
    args = parser.parse_args()

    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=ollama_host)
    os.makedirs(CHROMA_PATH, exist_ok=True)
    vectorstore = Chroma(
        persist_directory=CHROMA_PATH, embedding_function=embeddings, collection_name="zettabrain_docs"
    )

    # ---- Stats ----
    if args.stats:
        count = vectorstore._collection.count()
        hash_cache = load_hash_cache()
        print(f"\nVector store : {CHROMA_PATH}")
        print(f"Total chunks : {count}")
        print(f"Tracked files: {len(hash_cache)}")
        for fp in sorted(hash_cache):
            print(f"  - {Path(fp).name}")
        print()
        return

    # ---- Clear ----
    if args.clear:
        confirm = input("This will delete ALL ingested documents. Type 'yes' to confirm: ")
        if confirm.lower() == "yes":
            vectorstore._client.delete_collection("zettabrain_docs")
            save_hash_cache({})
            print("Vector store cleared.")
        else:
            print("Cancelled.")
        return

    # ---- Ingest ----
    hash_cache = load_hash_cache()
    if args.rebuild:
        hash_cache = {}
        save_hash_cache(hash_cache)
        print("Rebuild mode: hash cache cleared, all files will be re-ingested.")
    ingested = 0

    if args.file:
        profile = _get_storage_profile(str(Path(args.file).parent))
        print(f"\nIngesting file: {args.file}")
        print(f"Storage: {profile['storage_type']}")
        if ingest_file(args.file, vectorstore, hash_cache, profile):
            ingested += 1

    else:
        folder = Path(args.folder)
        if not folder.exists():
            print(f"ERROR: Folder not found: {folder}")
            print(f"Check the path exists: ls {DOCS_FOLDER}")
            return

        profile = _get_storage_profile(str(folder))
        files = [f for f in folder.rglob("*") if f.suffix.lower() in SUPPORTED]
        print(f"\nFound {len(files)} supported file(s) in {folder}")
        print(f"Storage: {profile['storage_type']}  file-batch={profile['batch_size']}")

        batch_count = 0
        for f in sorted(files):
            try:
                if ingest_file(str(f), vectorstore, hash_cache, profile):
                    ingested += 1
                    batch_count += 1
            except Exception as e:
                reason = f"unexpected error: {e}"
                print(f"  [FAIL] {f.name} — {reason}")
                log_ingest_error(str(f), reason)

            # Checkpoint hash cache every batch_size files so a crash doesn't
            # force re-ingestion of already-processed files.
            if batch_count > 0 and batch_count % profile["batch_size"] == 0:
                save_hash_cache(hash_cache)

    save_hash_cache(hash_cache)
    print(f"\nDone. {ingested} new file(s) ingested.")
    print(f"Total chunks in store: {vectorstore._collection.count()}")

    if ingested > 0 and _HAS_RETRIEVAL:
        print("Rebuilding BM25 keyword index...")
        n = rebuild_bm25_index(vectorstore)
        print(f"BM25 index: {n} chunks indexed.")


if __name__ == "__main__":
    main()
