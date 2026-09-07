"""Extract-Compute-Format pipeline for deterministic document generation.

Skills with `deterministic: true` use a three-step pipeline:
1. EXTRACT — LLM reads user request + corpus → structured JSON
2. COMPUTE — Python does all arithmetic with Decimal precision
3. FORMAT — LLM takes pre-computed numbers + skill template → final document
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

# ── Identify Schema (DB-lookup path) ─────────────────────────────────────────


class IdentifiedItem(BaseModel):
    description: str
    sku: str = ""
    quantity: Decimal
    unit: str = ""


class IdentifiedRequest(BaseModel):
    items: list[IdentifiedItem] = Field(default_factory=list)
    customer: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

# ── Extraction Schema ─────────────────────────────────────────────────────────


class LineItem(BaseModel):
    description: str
    unit: str = ""
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal = Decimal("0")
    discount_reason: str = ""
    source_ref: str = ""


class FeeItem(BaseModel):
    description: str
    amount: Decimal
    source_ref: str = ""


class TaxSpec(BaseModel):
    description: str
    rate_percent: Decimal
    source_ref: str = ""


class ExtractedData(BaseModel):
    line_items: list[LineItem] = Field(default_factory=list)
    fees: list[FeeItem] = Field(default_factory=list)
    taxes: list[TaxSpec] = Field(default_factory=list)
    customer: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Computed Result ───────────────────────────────────────────────────────────


class ComputedResult(BaseModel):
    line_details: list[dict[str, Any]]
    fee_details: list[dict[str, Any]]
    tax_details: list[dict[str, Any]]
    product_subtotal: Decimal
    total_discounts: Decimal
    discounted_subtotal: Decimal
    total_fees: Decimal
    subtotal_before_tax: Decimal
    total_tax: Decimal
    grand_total: Decimal
    computation_log: list[str]
    customer: dict[str, str]
    metadata: dict[str, Any]


# ── Prompts ───────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """You are a data extraction assistant. Your ONLY job is to read the documents and user request below, then output a JSON object. Do NOT generate a quote, letter, or any other document. Output ONLY valid JSON.

# CORPUS DOCUMENTS
{corpus_context}

# USER REQUEST
{user_input}

# YOUR TASK
Extract the following from the corpus documents and user request. Output a single JSON object with these keys:

{{
  "line_items": [
    {{
      "description": "product or service name",
      "unit": "unit of measure (lb, hour, each, etc.)",
      "quantity": <number>,
      "unit_price": <number from corpus>,
      "discount_percent": <number, 0 if none applies>,
      "discount_reason": "why this discount applies, or empty string",
      "source_ref": "which corpus document this price came from"
    }}
  ],
  "fees": [
    {{
      "description": "fee name (delivery, environmental, cylinder, etc.)",
      "amount": <number from corpus>,
      "source_ref": "which corpus document"
    }}
  ],
  "taxes": [
    {{
      "description": "tax name",
      "rate_percent": <number, e.g. 5.3 for 5.3%>,
      "source_ref": "which corpus document"
    }}
  ],
  "customer": {{
    "name": "customer/company name from user request",
    "contact": "contact person name if given",
    "address": "address if given",
    "phone": "phone if given"
  }},
  "metadata": {{
    "delivery_address": "delivery address if different from customer address",
    "delivery_speed": "standard, next_day, same_day, or emergency",
    "delivery_zone": "zone name/number if determinable from corpus",
    "notes": "any special requirements from the request"
  }}
}}

RULES:
1. Every unit_price, fee amount, discount percentage, and tax rate MUST come from the corpus documents. Quote them exactly as numbers.
2. Quantities and customer details come from the user request.
3. If a volume discount tier applies based on the ordered quantity matching a threshold in the corpus, set discount_percent and discount_reason.
4. Include ALL applicable fees from the corpus (delivery, environmental, cylinder, service fees, etc.).
5. If a value is not found in the corpus or user request, omit that item entirely. Do NOT invent prices.
6. All numbers must be plain numbers with no dollar signs, commas, or currency symbols.
7. Output ONLY the JSON object. No markdown fences, no explanation, no text before or after.

JSON:"""


_REPAIR_PROMPT = """The previous response was not valid JSON. Here is what you returned:

{raw_output}

Please fix it and return ONLY a valid JSON object with these keys: line_items, fees, taxes, customer, metadata.
Every number must be a plain number (no dollar signs, no commas). Output ONLY the JSON, nothing else.

JSON:"""


_FORMAT_PROMPT = """You are formatting a document. All monetary calculations have already been done for you.
Your job is ONLY to format the data below into the document structure specified in the task instructions.
Do NOT perform any arithmetic. Use the exact numbers provided.

# TASK INSTRUCTIONS
{skill_instructions}

# CORPUS DOCUMENTS (for reference — terms, conditions, contact info, boilerplate)
{corpus_context}

# USER REQUEST
{user_input}

# COMPUTED DATA — use these exact numbers, do NOT recalculate
{computed_summary}

# FORMATTING RULES
1. Use EVERY number from the COMPUTED DATA section exactly as shown. Do not round, truncate, or recalculate any amount.
2. The GRAND TOTAL is {grand_total}. This number is final and correct. Do not compute a different total.
3. Follow the document structure from TASK INSTRUCTIONS exactly.
4. Fill in customer information, dates, and boilerplate from the corpus and user request.
5. If a value is marked [NEEDS INPUT], keep that marker in the output.

Begin formatting the document now:"""


# ── JSON Parsing ──────────────────────────────────────────────────────────────


def parse_extraction(raw: str) -> Optional[ExtractedData]:
    """Parse LLM output into ExtractedData. Returns None on any failure."""
    text = raw.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    text = text[start : end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    try:
        extracted = ExtractedData.model_validate(data)
    except (ValidationError, InvalidOperation):
        return None

    if not extracted.line_items:
        return None

    return extracted


# ── Computation ───────────────────────────────────────────────────────────────

TWO_PLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    """Quantize to 2 decimal places."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_totals(extracted: ExtractedData) -> ComputedResult:
    """Deterministic arithmetic on extracted data. All currency in Decimal."""
    log: list[str] = []
    line_details: list[dict[str, Any]] = []

    product_subtotal = Decimal("0")
    total_discounts = Decimal("0")

    for i, item in enumerate(extracted.line_items, 1):
        line_total = _q(item.quantity * item.unit_price)
        log.append(f"Line {i}: {item.quantity} {item.unit} x ${item.unit_price} = ${line_total}")

        discount_amount = Decimal("0")
        if item.discount_percent > 0:
            discount_amount = _q(line_total * item.discount_percent / Decimal("100"))
            log.append(f"  Discount: {item.discount_percent}% = -${discount_amount} ({item.discount_reason})")

        net_total = line_total - discount_amount
        product_subtotal += line_total
        total_discounts += discount_amount

        line_details.append({
            "description": item.description,
            "unit": item.unit,
            "quantity": str(item.quantity),
            "unit_price": str(item.unit_price),
            "line_total": str(line_total),
            "discount_percent": str(item.discount_percent),
            "discount_amount": str(discount_amount),
            "discount_reason": item.discount_reason,
            "net_total": str(net_total),
            "source_ref": item.source_ref,
        })

    discounted_subtotal = product_subtotal - total_discounts
    log.append(f"Product subtotal: ${product_subtotal}")
    if total_discounts > 0:
        log.append(f"Total discounts: -${total_discounts}")
        log.append(f"Discounted subtotal: ${discounted_subtotal}")

    total_fees = Decimal("0")
    fee_details: list[dict[str, Any]] = []
    for fee in extracted.fees:
        amount = _q(fee.amount)
        total_fees += amount
        fee_details.append({
            "description": fee.description,
            "amount": str(amount),
            "source_ref": fee.source_ref,
        })
        log.append(f"Fee: {fee.description} = ${amount}")

    subtotal_before_tax = discounted_subtotal + total_fees
    log.append(f"Subtotal before tax: ${subtotal_before_tax}")

    total_tax = Decimal("0")
    tax_details: list[dict[str, Any]] = []
    for tax in extracted.taxes:
        tax_base = subtotal_before_tax
        tax_amount = _q(tax_base * tax.rate_percent / Decimal("100"))
        total_tax += tax_amount
        tax_details.append({
            "description": tax.description,
            "rate_percent": str(tax.rate_percent),
            "tax_base": str(tax_base),
            "tax_amount": str(tax_amount),
            "source_ref": tax.source_ref,
        })
        log.append(f"Tax: {tax.description} ({tax.rate_percent}% of ${tax_base}) = ${tax_amount}")

    grand_total = _q(subtotal_before_tax + total_tax)
    log.append(f"GRAND TOTAL: ${grand_total}")

    return ComputedResult(
        line_details=line_details,
        fee_details=fee_details,
        tax_details=tax_details,
        product_subtotal=_q(product_subtotal),
        total_discounts=_q(total_discounts),
        discounted_subtotal=_q(discounted_subtotal),
        total_fees=_q(total_fees),
        subtotal_before_tax=_q(subtotal_before_tax),
        total_tax=_q(total_tax),
        grand_total=grand_total,
        computation_log=log,
        customer=extracted.customer,
        metadata=extracted.metadata,
    )


# ── Corpus Validation ─────────────────────────────────────────────────────────


def _normalise_number(value: str) -> set[str]:
    """Return several normalised forms of a numeric string for fuzzy corpus matching."""
    forms: set[str] = {value}
    # Remove thousands commas: "420,000" → "420000"
    no_comma = value.replace(",", "")
    forms.add(no_comma)
    # Add thousands commas to a plain number: "420000" → "420,000"
    try:
        n = float(no_comma)
        forms.add(f"{n:,.0f}")
        forms.add(f"{n:,.2f}")
        forms.add(str(int(n)) if n == int(n) else str(n))
    except ValueError:
        pass
    return forms


def validate_against_corpus(extracted: ExtractedData, corpus_text: str) -> list[str]:
    """Check that extracted prices appear in the corpus. Returns advisory warnings.

    Number normalisation handles thousands separators so '420000' matches '420,000'.
    """
    warnings: list[str] = []
    corpus_lower = corpus_text.lower()

    for item in extracted.line_items:
        forms = _normalise_number(str(item.unit_price))
        found = any(f in corpus_lower or f"₦{f}" in corpus_lower or f"${f}" in corpus_lower for f in forms)
        if not found:
            warnings.append(
                f"Unit price {item.unit_price} for '{item.description}' was not found in the corpus. "
                "Verify this price is correct."
            )

    for fee in extracted.fees:
        forms = _normalise_number(str(fee.amount))
        found = any(f in corpus_lower or f"₦{f}" in corpus_lower or f"${f}" in corpus_lower for f in forms)
        if not found:
            warnings.append(f"Fee {fee.amount} for '{fee.description}' was not found in the corpus.")

    for tax in extracted.taxes:
        forms = _normalise_number(str(tax.rate_percent))
        found = any(f in corpus_lower for f in forms)
        if not found:
            warnings.append(f"Tax rate {tax.rate_percent}% for '{tax.description}' was not found in the corpus.")

    return warnings


# ── Prompt Builders ───────────────────────────────────────────────────────────


def build_extraction_prompt(corpus_context: str, user_input: str) -> str:
    return _EXTRACTION_PROMPT.format(corpus_context=corpus_context, user_input=user_input)


def build_repair_prompt(raw_output: str) -> str:
    return _REPAIR_PROMPT.format(raw_output=raw_output[:2000])


def build_computed_summary(computed: ComputedResult) -> str:
    """Format computed results into a human-readable block for the format prompt."""
    parts = ["## Customer"]
    for key, val in computed.customer.items():
        if val:
            parts.append(f"- {key}: {val}")

    parts.append("\n## Line Items")
    parts.append("| # | Description | Qty | Unit | Unit Price | Line Total | Discount | Net |")
    parts.append("|---|-------------|-----|------|------------|------------|----------|-----|")
    for i, ld in enumerate(computed.line_details, 1):
        disc = f"-${ld['discount_amount']}" if Decimal(ld["discount_amount"]) > 0 else "-"
        parts.append(
            f"| {i} | {ld['description']} | {ld['quantity']} | {ld['unit']} "
            f"| ${ld['unit_price']} | ${ld['line_total']} | {disc} | ${ld['net_total']} |"
        )

    if computed.fee_details:
        parts.append("\n## Fees")
        for fd in computed.fee_details:
            parts.append(f"- {fd['description']}: ${fd['amount']}")

    if computed.tax_details:
        parts.append("\n## Taxes")
        for td in computed.tax_details:
            parts.append(f"- {td['description']}: {td['rate_percent']}% of ${td['tax_base']} = ${td['tax_amount']}")

    parts.append("\n## Calculation Summary")
    for line in computed.computation_log:
        parts.append(line)

    if computed.metadata:
        delivery_info = []
        if computed.metadata.get("delivery_address"):
            delivery_info.append(f"Delivery address: {computed.metadata['delivery_address']}")
        if computed.metadata.get("delivery_speed"):
            delivery_info.append(f"Delivery speed: {computed.metadata['delivery_speed']}")
        if computed.metadata.get("delivery_zone"):
            delivery_info.append(f"Delivery zone: {computed.metadata['delivery_zone']}")
        if computed.metadata.get("notes"):
            delivery_info.append(f"Notes: {computed.metadata['notes']}")
        if delivery_info:
            parts.append("\n## Delivery & Notes")
            for info in delivery_info:
                parts.append(f"- {info}")

    return "\n".join(parts)


def build_format_prompt(
    skill_instructions: str,
    corpus_context: str,
    user_input: str,
    computed: ComputedResult,
) -> str:
    summary = build_computed_summary(computed)
    return _FORMAT_PROMPT.format(
        skill_instructions=skill_instructions,
        corpus_context=corpus_context or "(no additional corpus context)",
        user_input=user_input,
        computed_summary=summary,
        grand_total=f"${computed.grand_total}",
    )


# ── Identify prompt (DB-lookup path) ─────────────────────────────────────────

_IDENTIFY_PROMPT = """You are extracting order details from a customer request. Your ONLY job is to identify which products or services the customer wants and in what quantity. Do NOT look up, guess, or invent prices — prices will be retrieved from a separate database.

# CUSTOMER REQUEST
{user_input}

# YOUR TASK
Extract what was ordered. Output a single JSON object with these keys:

{{
  "items": [
    {{
      "description": "product or service name exactly as the customer described it",
      "sku": "product code if explicitly mentioned (e.g. SV-003), or empty string",
      "quantity": <number>,
      "unit": "unit of measure if stated (month, each, hour, sqft, etc.), or empty string"
    }}
  ],
  "customer": {{
    "name": "customer or company name",
    "contact": "contact person name if given",
    "address": "address if given",
    "phone": "phone if given"
  }},
  "metadata": {{
    "delivery_address": "delivery address if different from customer address, else empty",
    "notes": "payment terms, account type, start date, billing cycle, or other requirements"
  }}
}}

RULES:
1. List ONLY products/services explicitly requested — do not add extras.
2. Convert word quantities to numbers ("three floors" → 3, "a pair" → 2).
3. Do NOT estimate or invent prices, unit costs, fees, or taxes.
4. Output ONLY the JSON object. No markdown, no explanation.

JSON:"""


def build_identify_prompt(user_input: str) -> str:
    return _IDENTIFY_PROMPT.format(user_input=user_input)


def parse_identification(raw: str) -> Optional[IdentifiedRequest]:
    """Parse LLM output from the identify step into an IdentifiedRequest."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        req = IdentifiedRequest.model_validate(data)
    except (ValidationError, InvalidOperation):
        return None
    if not req.items:
        return None
    return req


def lookup_prices_from_db(
    identified: IdentifiedRequest,
    source_files: Optional[list[str]] = None,
) -> tuple[ExtractedData, list[str]]:
    """Look up prices from the SQLite price list DB.

    Returns (ExtractedData with prices filled in, list of warning strings).
    Items not found in the DB are omitted from line_items and generate a warning.
    """
    from ..price_list import search_by_sku, search_items  # noqa: PLC0415

    warnings: list[str] = []
    line_items: list[LineItem] = []
    source_file = source_files[0] if source_files else ""

    for item in identified.items:
        db_row: Optional[dict] = None

        # 1. Exact SKU lookup
        if item.sku:
            db_row = search_by_sku(item.sku, source_file)

        # 2. FTS5 trigram search by full description
        if db_row is None:
            results = search_items(item.description, source_file, limit=3)
            if results:
                db_row = results[0]

        # 3. Shorter query (first 3 significant words) as fallback
        if db_row is None:
            words = [w for w in item.description.split() if len(w) > 2][:3]
            short_query = " ".join(words)
            if short_query and short_query.lower() != item.description.lower():
                results = search_items(short_query, source_file, limit=3)
                if results:
                    db_row = results[0]

        if db_row is None:
            warnings.append(
                f"'{item.description}' was not found in the price list. "
                "Check that the product name matches the price list exactly, then re-ingest."
            )
            continue

        try:
            unit_price = Decimal(str(db_row["base_price"]))
        except (InvalidOperation, TypeError):
            warnings.append(f"Invalid price for '{db_row['name']}' in price list DB — skipping.")
            continue

        line_items.append(
            LineItem(
                description=db_row["name"],
                unit=item.unit or db_row.get("unit", ""),
                quantity=Decimal(str(item.quantity)),
                unit_price=unit_price,
                discount_percent=Decimal("0"),
                source_ref=db_row.get("source_file", ""),
            )
        )

    return (
        ExtractedData(
            line_items=line_items,
            customer=identified.customer,
            metadata=identified.metadata,
        ),
        warnings,
    )
