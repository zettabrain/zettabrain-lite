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


def validate_against_corpus(extracted: ExtractedData, corpus_text: str) -> list[str]:
    """Check that extracted prices appear in the corpus. Returns advisory warnings."""
    warnings: list[str] = []
    corpus_lower = corpus_text.lower()

    for item in extracted.line_items:
        price_str = str(item.unit_price)
        if price_str not in corpus_lower and f"${price_str}" not in corpus_lower:
            warnings.append(
                f"Unit price ${price_str} for '{item.description}' was not found in the corpus. "
                f"Verify this price is correct."
            )

    for fee in extracted.fees:
        fee_str = str(fee.amount)
        if fee_str not in corpus_lower and f"${fee_str}" not in corpus_lower:
            warnings.append(f"Fee ${fee_str} for '{fee.description}' was not found in the corpus.")

    for tax in extracted.taxes:
        rate_str = str(tax.rate_percent)
        if rate_str not in corpus_lower:
            warnings.append(f"Tax rate {rate_str}% for '{tax.description}' was not found in the corpus.")

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
        corpus_context=corpus_context,
        user_input=user_input,
        computed_summary=summary,
        grand_total=f"${computed.grand_total}",
    )
