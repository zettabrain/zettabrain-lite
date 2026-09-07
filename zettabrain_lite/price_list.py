"""ZettaBrain Lite — Structured price list storage and lookup via SQLite FTS5."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Optional

from .config import DATABASE_PATH

log = logging.getLogger(__name__)

# ── Currency symbol → ISO code ────────────────────────────────────────────────
_CURRENCY_SYMBOLS: dict[str, str] = {
    "₦": "NGN",
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "R": "ZAR",
}
_CURRENCY_CODES = {"NGN", "USD", "GBP", "EUR", "JPY", "INR", "CAD", "AUD", "ZAR", "GHS", "KES"}

# ── Column name keyword buckets ───────────────────────────────────────────────
_NAME_KW = {"name", "product", "service", "item", "description", "title"}
_SKU_KW = {"sku", "code", "ref", "number", "catalog", "no", "id"}
_PRICE_KW = {"price", "cost", "rate", "amount", "fee", "charge"}
_UNIT_KW = {"unit", "uom", "per", "measure", "billing"}
_CATEGORY_KW = {"category", "type", "group", "class", "dept", "department"}
_CURRENCY_KW = {"currency", "ccy", "curr"}
_NOTES_KW = {"note", "notes", "remark", "remarks", "comment", "comments", "condition", "terms"}

# ── Schema (FTS5 trigram requires SQLite ≥ 3.35, ships with Python ≥ 3.10) ───
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS price_list_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file    TEXT    NOT NULL,
    sku            TEXT    DEFAULT '',
    name           TEXT    NOT NULL,
    description    TEXT    DEFAULT '',
    unit           TEXT    DEFAULT '',
    base_price     REAL    NOT NULL,
    currency       TEXT    DEFAULT '',
    category       TEXT    DEFAULT '',
    min_qty        REAL    DEFAULT 1.0,
    price_tiers    TEXT    DEFAULT '[]',
    discount_rules TEXT    DEFAULT '[]',
    notes          TEXT    DEFAULT '',
    ingested_at    TEXT    DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS price_list_fts USING fts5(
    name,
    sku UNINDEXED,
    description,
    content='price_list_items',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS pli_ai AFTER INSERT ON price_list_items BEGIN
    INSERT INTO price_list_fts(rowid, name, sku, description)
    VALUES (new.id, new.name, new.sku, new.description);
END;
CREATE TRIGGER IF NOT EXISTS pli_ad AFTER DELETE ON price_list_items BEGIN
    INSERT INTO price_list_fts(price_list_fts, rowid, name, sku, description)
    VALUES ('delete', old.id, old.name, old.sku, old.description);
END;
CREATE TRIGGER IF NOT EXISTS pli_au AFTER UPDATE ON price_list_items BEGIN
    INSERT INTO price_list_fts(price_list_fts, rowid, name, sku, description)
    VALUES ('delete', old.id, old.name, old.sku, old.description);
    INSERT INTO price_list_fts(rowid, name, sku, description)
    VALUES (new.id, new.name, new.sku, new.description);
END;
"""


# ── Connection ────────────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables/triggers/FTS index if they don't exist yet."""
    conn.executescript(_SCHEMA_SQL)


def get_price_db() -> sqlite3.Connection:
    """Return an open connection with the price list schema ensured."""
    conn = _connect()
    _ensure_schema(conn)
    return conn


# ── Column detection ──────────────────────────────────────────────────────────


def _words(header: str) -> list[str]:
    """Split a header into lowercase words, stripping punctuation."""
    return re.sub(r"[^a-z0-9]", " ", header.lower()).split()


def _header_has_word(header: str, keywords: set[str]) -> bool:
    """True if any keyword appears as a complete word in the header."""
    return bool(set(_words(header)) & keywords)


def _header_contains(header: str, keywords: set[str]) -> bool:
    """True if any keyword is a substring of the normalised header."""
    h = re.sub(r"[^a-z0-9]", "", header.lower())
    return any(kw.replace(" ", "") in h for kw in keywords)


def detect_columns(headers: list[str]) -> dict[str, int]:
    """Map canonical field names to 0-based column indices.

    Detection order per column: sku → price → unit → currency → category → notes → name → description.
    SKU/price are checked before name so headers like 'Service Code' resolve to sku, not name.

    Returns at minimum {'name': i, 'price': j} when detected.
    Returns {} if name or price cannot be identified.
    """
    result: dict[str, int] = {}
    for i, raw in enumerate(headers):
        if raw is None:
            continue
        h = str(raw)

        # High-specificity buckets first so they win over looser name/description matches
        if "sku" not in result and (_header_has_word(h, _SKU_KW) or _header_contains(h, {"sku", "catalog"})):
            result["sku"] = i
            continue
        if "price" not in result and _header_has_word(h, _PRICE_KW):
            result["price"] = i
            continue
        if "unit" not in result and _header_has_word(h, _UNIT_KW):
            result["unit"] = i
            continue
        if "currency" not in result and _header_has_word(h, _CURRENCY_KW):
            result["currency"] = i
            continue
        if "category" not in result and _header_has_word(h, _CATEGORY_KW):
            result["category"] = i
            continue
        if "notes" not in result and _header_has_word(h, _NOTES_KW):
            result["notes"] = i
            continue
        # Name / description (checked last — broadest match)
        if "name" not in result and _header_has_word(h, _NAME_KW):
            result["name"] = i
        elif "description" not in result and "name" in result and _header_has_word(h, _NAME_KW):
            result["description"] = i

    if "name" not in result or "price" not in result:
        return {}
    return result


# ── Price value parsing ───────────────────────────────────────────────────────


def parse_price(value: object) -> tuple[Optional[float], str]:
    """Parse a cell value to (price_float, currency_code).

    Returns (None, '') when the value is not a valid price.
    """
    if value is None:
        return None, ""
    s = str(value).strip()
    if not s or s.lower() in {"n/a", "na", "-", "tbc", "tbd", "quote", "varies", "call"}:
        return None, ""

    currency = ""
    # Detect currency symbol
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in s:
            currency = code
            s = s.replace(sym, "").strip()
            break

    # Detect ISO code prefix (e.g. "NGN 420000" or "USD420")
    if not currency:
        upper = s.upper()
        for code in _CURRENCY_CODES:
            if upper.startswith(code):
                currency = code
                s = s[len(code):].strip()
                break

    # Remove thousands separators and whitespace
    s = re.sub(r"[,\s]", "", s)
    # Take lower bound of a range ("420000-500000" → "420000")
    if "-" in s:
        s = s.split("-")[0]
    # Drop unit suffix ("/month", "/each", etc.)
    s = re.sub(r"/.*$", "", s)

    try:
        return float(s), currency
    except ValueError:
        return None, ""


# ── DB write ──────────────────────────────────────────────────────────────────


def upsert_items(source_file: str, rows: list[dict]) -> int:
    """Replace all items for *source_file* and insert *rows*.

    Each dict must have at least 'name' and 'base_price'.
    Returns the number of rows inserted.
    """
    conn = get_price_db()
    try:
        conn.execute("DELETE FROM price_list_items WHERE source_file = ?", (source_file,))
        count = 0
        for row in rows:
            price, detected_currency = parse_price(row.get("base_price"))
            if price is None:
                continue
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            conn.execute(
                """INSERT INTO price_list_items
                   (source_file, sku, name, description, unit, base_price, currency,
                    category, min_qty, price_tiers, discount_rules, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_file,
                    str(row.get("sku", "")).strip(),
                    name,
                    str(row.get("description", "")).strip(),
                    str(row.get("unit", "")).strip(),
                    price,
                    row.get("currency", detected_currency) or detected_currency,
                    str(row.get("category", "")).strip(),
                    float(row.get("min_qty", 1.0)),
                    json.dumps(row.get("price_tiers", [])),
                    json.dumps(row.get("discount_rules", [])),
                    str(row.get("notes", "")).strip(),
                ),
            )
            count += 1
        conn.commit()
        log.info("Price list upsert: %d rows for %s", count, source_file)
        return count
    finally:
        conn.close()


# ── DB read ───────────────────────────────────────────────────────────────────


def search_items(query: str, source_file: str = "", limit: int = 5) -> list[dict]:
    """FTS5 trigram search across name + description. Optionally filter by source_file."""
    if not query or not query.strip():
        return []
    conn = get_price_db()
    try:
        # Escape FTS5 special chars
        safe = re.sub(r'["\*\(\)\:\.\^]', " ", query).strip()
        if not safe:
            return []
        sql = """
            SELECT p.*, rank
            FROM price_list_fts f
            JOIN price_list_items p ON p.id = f.rowid
            WHERE price_list_fts MATCH ?
        """
        params: list[object] = [safe]
        if source_file:
            sql += " AND p.source_file = ?"
            params.append(source_file)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("FTS5 search error for %r: %s", query, exc)
        return []
    finally:
        conn.close()


def search_by_sku(sku: str, source_file: str = "") -> Optional[dict]:
    """Direct exact SKU lookup."""
    if not sku or not sku.strip():
        return None
    conn = get_price_db()
    try:
        sql = "SELECT * FROM price_list_items WHERE LOWER(sku) = LOWER(?)"
        params: list[object] = [sku.strip()]
        if source_file:
            sql += " AND source_file = ?"
            params.append(source_file)
        sql += " LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_items(source_file: str = "") -> list[dict]:
    """Return all price list items, optionally filtered by source_file."""
    conn = get_price_db()
    try:
        if source_file:
            rows = conn.execute(
                "SELECT * FROM price_list_items WHERE source_file = ? ORDER BY name",
                (source_file,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM price_list_items ORDER BY source_file, name"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_source_files() -> list[dict]:
    """Return [{source_file, item_count}] for all ingested price lists."""
    conn = get_price_db()
    try:
        rows = conn.execute(
            "SELECT source_file, COUNT(*) AS item_count "
            "FROM price_list_items GROUP BY source_file ORDER BY source_file"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def has_price_data(source_files: Optional[list[str]] = None) -> bool:
    """Return True if any price list items exist (optionally scoped to source_files)."""
    conn = get_price_db()
    try:
        if source_files:
            placeholders = ",".join("?" * len(source_files))
            row = conn.execute(
                f"SELECT COUNT(*) FROM price_list_items WHERE source_file IN ({placeholders})",
                source_files,
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM price_list_items").fetchone()
        return bool(row and row[0] > 0)
    finally:
        conn.close()


def delete_source(source_file: str) -> int:
    """Delete all items for a source file. Returns count deleted."""
    conn = get_price_db()
    try:
        cur = conn.execute("DELETE FROM price_list_items WHERE source_file = ?", (source_file,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
