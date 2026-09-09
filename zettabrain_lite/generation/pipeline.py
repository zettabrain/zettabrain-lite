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
    # What the customer says their account is ("corporate", "trade", ...). The rate itself is
    # never taken from the model — it is looked up from the price list by this name.
    account_type: str = ""

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


class LineDiscountSpec(BaseModel):
    """An account-tier rate applied to every line, as the price list's tier columns do."""

    description: str
    rate_percent: Decimal


class OrderDiscount(BaseModel):
    """A discount applied to the order as a whole rather than to a single line."""

    description: str
    rate_percent: Decimal
    threshold: Decimal = Decimal("0")  # only applies once the subtotal reaches this
    source_ref: str = ""


class ExtractedData(BaseModel):
    line_items: list[LineItem] = Field(default_factory=list)
    fees: list[FeeItem] = Field(default_factory=list)
    taxes: list[TaxSpec] = Field(default_factory=list)
    order_discounts: list[OrderDiscount] = Field(default_factory=list)
    customer: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    currency: str = ""  # ISO 4217 code detected from price list DB or skill frontmatter


# ── Computed Result ───────────────────────────────────────────────────────────


class ComputedResult(BaseModel):
    line_details: list[dict[str, Any]]
    fee_details: list[dict[str, Any]]
    tax_details: list[dict[str, Any]]
    order_discount_details: list[dict[str, Any]] = Field(default_factory=list)
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
    currency: str = ""  # ISO 4217 code passed through from ExtractedData


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
      "rate_percent": <the tax rate as a number, taken from the corpus>,
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
6. Never copy a number from these instructions. Every figure must come from the corpus or the user request.
7. All numbers must be plain numbers with no dollar signs, commas, or currency symbols.
8. Output ONLY the JSON object. No markdown fences, no explanation, no text before or after.

JSON:"""


_REPAIR_PROMPT = """The previous response was not valid JSON. Here is what you returned:

{raw_output}

Please fix it and return ONLY a valid JSON object with these keys: line_items, fees, taxes, customer, metadata.
Every number must be a plain number (no dollar signs, no commas). Output ONLY the JSON, nothing else.

JSON:"""


_FORMAT_PROMPT = """You are writing the final document. All monetary calculations are already done.
Your job is to write the document described in the task instructions, using the supplied values.
Do NOT perform any arithmetic.

Today's date is {today}. Use it for any date the document needs. Never invent a different date.

# YOUR ORGANISATION (the sender of this document)
{business_identity}

# TASK INSTRUCTIONS
{skill_instructions}

# CORPUS DOCUMENTS (for reference — terms, conditions, boilerplate)
{corpus_context}

# USER REQUEST
{user_input}

# COMPUTED FIGURES — reference data, NOT document text
The block below is a set of values for you to use. It is NOT part of the document and its
layout is NOT a template. Never copy its keys, its uppercase headings, its "item_1.xxx" names
or its structure into your output. Read the values, then present them in the format the task
instructions describe.

{computed_summary}

# FORMATTING RULES
1. Use EVERY number from the COMPUTED DATA section exactly as shown. Do not round, truncate, or recalculate any amount.
2. The GRAND TOTAL is {grand_total}. This number is final and correct. Do not compute a different total.
3. Follow the document structure from TASK INSTRUCTIONS exactly.
4. Fill in customer information, dates, and boilerplate from the corpus and user request.
5. If a value is marked [NEEDS INPUT], keep that marker in the output.
6. Do NOT add any line item, fee, charge, surcharge, or tax that is absent from COMPUTED FIGURES. The items,
   fees, discounts and taxes listed there are the complete and final set. If the task instructions mention a
   charge that COMPUTED FIGURES does not contain — delivery, installation, service, or anything else — write
   [NEEDS INPUT] in place of the amount. Never estimate it, and never carry a figure over from the corpus.
7. Do NOT introduce a total, subtotal, or discount that is not shown in COMPUTED FIGURES.
8. Never state or imply that a discount, rate or adjustment was applied unless it appears in ORDER_DISCOUNTS
   or as a line item discount. If the customer asked for a rate that is not there, say plainly that it has
   not been applied. Never write "corporate rate applied" when no discount appears in the figures.
9. Write the document once. Do not repeat a section, restate the totals in a second block, or append a
   summary of the values you were given.
10. Do not include a workings or calculation-summary section. The reader wants the figures, not the steps.

Begin writing the document now:"""


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


# ── Currency symbol lookup ─────────────────────────────────────────────────────

_ISO_TO_SYM: dict[str, str] = {
    "NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥",
    "INR": "₹", "GHS": "GH₵", "KES": "KSh", "ZAR": "R",
    "AUD": "A$", "CAD": "C$",
}


def _sym(currency: str) -> str:
    """Return the display symbol for an ISO currency code, e.g. 'NGN' → '₦'."""
    return _ISO_TO_SYM.get(currency.upper(), "")


def money(amount: Decimal | str | float, currency: str = "") -> str:
    """Format an amount for display: 420000 → '₦420,000.00'.

    Prices arrive from SQLite as floats, so str() alone yields '420000.0'. Thousands
    separators matter on documents a customer reads.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return str(amount)
    prefix = _sym(currency) or (f"{currency.upper()} " if currency else "")
    return f"{prefix}{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def qty(amount: Decimal | str | float) -> str:
    """Format a quantity without trailing zeros: 3.0 → '3', 2.5 → '2.5'."""
    try:
        value = Decimal(str(amount)).normalize()
    except (InvalidOperation, TypeError, ValueError):
        return str(amount)
    return f"{value:f}"


# ── Computation ───────────────────────────────────────────────────────────────

TWO_PLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    """Quantize to 2 decimal places."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_totals(extracted: ExtractedData) -> ComputedResult:
    """Deterministic arithmetic on extracted data. All currency in Decimal."""
    log: list[str] = []
    line_details: list[dict[str, Any]] = []
    cur = extracted.currency

    product_subtotal = Decimal("0")
    total_discounts = Decimal("0")

    for i, item in enumerate(extracted.line_items, 1):
        line_total = _q(item.quantity * item.unit_price)
        log.append(
            f"Line {i}: {qty(item.quantity)} {item.unit} x {money(item.unit_price, cur)} "
            f"= {money(line_total, cur)}"
        )

        discount_amount = Decimal("0")
        if item.discount_percent > 0:
            discount_amount = _q(line_total * item.discount_percent / Decimal("100"))
            log.append(
                f"  Discount: {qty(item.discount_percent)}% = -{money(discount_amount, cur)} "
                f"({item.discount_reason})"
            )

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
    log.append(f"Product subtotal: {money(product_subtotal, cur)}")
    if total_discounts > 0:
        log.append(f"Line discounts: -{money(total_discounts, cur)}")
        log.append(f"Subtotal after line discounts: {money(discounted_subtotal, cur)}")

    # Order-level discounts (bulk tiers and similar) apply to the discounted subtotal.
    order_discount_details: list[dict[str, Any]] = []
    for od in extracted.order_discounts:
        if od.threshold > 0 and discounted_subtotal < od.threshold:
            log.append(
                f"{od.description} not applied: subtotal {money(discounted_subtotal, cur)} "
                f"is below the {money(od.threshold, cur)} threshold"
            )
            continue
        amount = _q(discounted_subtotal * od.rate_percent / Decimal("100"))
        total_discounts += amount
        discounted_subtotal -= amount
        order_discount_details.append({
            "description": od.description,
            "rate_percent": str(od.rate_percent),
            "amount": str(amount),
            "source_ref": od.source_ref,
        })
        log.append(
            f"{od.description}: {qty(od.rate_percent)}% = -{money(amount, cur)} "
            f"(subtotal now {money(discounted_subtotal, cur)})"
        )

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
        log.append(f"Fee: {fee.description} = {money(amount, cur)}")

    subtotal_before_tax = discounted_subtotal + total_fees
    log.append(f"Subtotal before tax: {money(subtotal_before_tax, cur)}")

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
        log.append(
            f"Tax: {tax.description} ({qty(tax.rate_percent)}% of {money(tax_base, cur)}) "
            f"= {money(tax_amount, cur)}"
        )

    grand_total = _q(subtotal_before_tax + total_tax)
    log.append(f"GRAND TOTAL: {money(grand_total, cur)}")

    return ComputedResult(
        line_details=line_details,
        fee_details=fee_details,
        tax_details=tax_details,
        order_discount_details=order_discount_details,
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
        currency=extracted.currency,
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
    """Render computed results as a data block for the format prompt.

    Deliberately NOT markdown. An earlier version used '##' headings and a markdown table,
    and small models reproduced it into the customer's document verbatim — headings, internal
    calculation log and all. Data-shaped text with uppercase keys does not read as a finished
    document, so the model has to transform it rather than copy it. The calculation log is
    withheld from the prompt entirely; it is internal and is kept in the result metadata.
    """
    cur = computed.currency
    lines = [f"CURRENCY: {cur or 'unknown'} — render every amount in this currency, no other."]

    if computed.customer:
        lines.append("")
        lines.append("CUSTOMER")
        for key, val in computed.customer.items():
            if val:
                lines.append(f"  {key} = {val}")

    lines.append("")
    lines.append("LINE_ITEMS")
    for i, ld in enumerate(computed.line_details, 1):
        lines.append(f"  item_{i}.description = {ld['description']}")
        lines.append(f"  item_{i}.quantity    = {qty(ld['quantity'])}")
        if ld.get("unit"):
            lines.append(f"  item_{i}.unit        = {ld['unit']}")
        lines.append(f"  item_{i}.unit_price  = {money(ld['unit_price'], cur)}")
        if Decimal(ld["discount_amount"]) > 0:
            lines.append(
                f"  item_{i}.discount    = {qty(ld['discount_percent'])}% "
                f"(-{money(ld['discount_amount'], cur)}) {ld.get('discount_reason', '')}".rstrip()
            )
        lines.append(f"  item_{i}.line_total  = {money(ld['net_total'], cur)}")

    if computed.order_discount_details:
        lines.append("")
        lines.append("ORDER_DISCOUNTS  (show each as its own row in the totals)")
        for i, od in enumerate(computed.order_discount_details, 1):
            lines.append(
                f"  discount_{i} = {od['description']} | {qty(od['rate_percent'])}% "
                f"| -{money(od['amount'], cur)}"
            )

    if computed.fee_details:
        lines.append("")
        lines.append("FEES")
        for i, fd in enumerate(computed.fee_details, 1):
            lines.append(f"  fee_{i} = {fd['description']} | {money(fd['amount'], cur)}")

    if computed.tax_details:
        lines.append("")
        lines.append("TAXES")
        for i, td in enumerate(computed.tax_details, 1):
            lines.append(
                f"  tax_{i} = {td['description']} | {qty(td['rate_percent'])}% of "
                f"{money(td['tax_base'], cur)} | {money(td['tax_amount'], cur)}"
            )

    lines.append("")
    lines.append("TOTALS")
    lines.append(f"  subtotal      = {money(computed.product_subtotal, cur)}")
    if computed.total_discounts > 0:
        lines.append(f"  discounts     = -{money(computed.total_discounts, cur)}")
    if computed.total_fees > 0:
        lines.append(f"  fees          = {money(computed.total_fees, cur)}")
    if computed.total_tax > 0:
        lines.append(f"  tax           = {money(computed.total_tax, cur)}")
    lines.append(f"  GRAND_TOTAL   = {money(computed.grand_total, cur)}")

    meta_keys = ("delivery_address", "delivery_speed", "delivery_zone", "notes")
    supplied = [(k, computed.metadata.get(k)) for k in meta_keys if computed.metadata.get(k)]
    if supplied:
        lines.append("")
        lines.append("REQUEST_DETAILS")
        for key, val in supplied:
            lines.append(f"  {key} = {val}")

    return "\n".join(lines)


def build_format_prompt(
    skill_instructions: str,
    corpus_context: str,
    user_input: str,
    computed: ComputedResult,
    business_identity: str = "",
    today: str = "",
) -> str:
    from datetime import datetime  # noqa: PLC0415

    summary = build_computed_summary(computed)
    return _FORMAT_PROMPT.format(
        skill_instructions=skill_instructions,
        corpus_context=corpus_context or "(no additional corpus context)",
        user_input=user_input,
        computed_summary=summary,
        grand_total=money(computed.grand_total, computed.currency),
        business_identity=business_identity or "(not configured — write [NEEDS INPUT] where the "
        "document needs your organisation's name or contact details)",
        today=today or datetime.now().strftime("%d %B %Y"),
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
  "account_type": "the pricing tier the customer says they are on, in one lowercase word, e.g. corporate, trade, wholesale, retail. Empty string if they do not mention one.",
  "metadata": {{
    "delivery_address": "delivery address if different from customer address, else empty",
    "notes": "payment terms, start date, billing cycle, or other requirements"
  }}
}}

RULES:
1. List ONLY products/services explicitly requested — do not add extras.
2. Include delivery, installation or call-out as an item when the customer asks for it, using
   their own words ("delivery to Victoria Island"). Its price is looked up like any other item.
3. Convert word quantities to numbers ("three floors" → 3, "a pair" → 2).
4. Do NOT estimate or invent prices, unit costs, discount rates, fees, or taxes. account_type
   records only what the customer claims — never a percentage.
5. Output ONLY the JSON object. No markdown, no explanation.

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


# Words shared by most rate labels. They carry no identity, so they must not be what pairs a
# discount with a threshold — otherwise "Fleet account discount" pairs with "Volume discount
# threshold" on the word "discount" alone.
_STOP = {
    "the", "and", "of", "or", "a", "an", "above", "over", "orders", "order", "value",
    "rate", "rates", "applied", "all", "items", "customer", "customers",
    "discount", "discounts", "rebate", "threshold", "minimum", "surcharge", "markup",
    "tax", "vat", "gst", "hst", "account", "accounts", "level", "levels", "tier",
}


def _label_words(label: str) -> set[str]:
    """The identifying words of a rate label — what makes it 'bulk' or 'fleet' or 'trade'."""
    return {w for w in re.findall(r"[a-z]+", label.lower()) if len(w) > 2 and w not in _STOP}


def build_order_discounts(
    account_type: str,
    source_file: str,
) -> tuple[list[OrderDiscount], list[LineDiscountSpec], list[str]]:
    """Derive discounts from rates stated in the user's own price list.

    Rates are never taken from the model. The customer's claimed account type is matched by
    name against the discount labels the user wrote in their file, and threshold-based
    discounts are paired with a threshold row that shares wording with them.

    Returns (order_discounts, line_discounts, notes).
    """
    from ..price_list import get_settings  # noqa: PLC0415

    settings = get_settings(source_file)
    discounts = [s for s in settings if s.get("kind") == "discount" and s.get("percent") is not None]
    thresholds = [s for s in settings if s.get("kind") == "threshold" and s.get("amount") is not None]

    order: list[OrderDiscount] = []
    line: list[LineDiscountSpec] = []
    notes: list[str] = []

    account = account_type.strip().lower()
    for setting in discounts:
        words = _label_words(setting["label"])
        matched_threshold = next(
            (t for t in thresholds if _label_words(t["label"]) & words), None
        )
        if matched_threshold:
            # A tiered discount: Python decides whether the subtotal reaches the threshold.
            order.append(OrderDiscount(
                description=setting["label"],
                rate_percent=Decimal(str(setting["percent"])),
                threshold=Decimal(str(matched_threshold["amount"])),
                source_ref=source_file,
            ))
        elif account and account in words:
            # An account-tier rate, applied per line the way the price list's own tier
            # columns are calculated.
            line.append(LineDiscountSpec(
                description=setting["label"],
                rate_percent=Decimal(str(setting["percent"])),
            ))

    if account and not line:
        available = ", ".join(sorted({w for s in discounts for w in _label_words(s["label"])}))
        notes.append(
            f"The customer asked for the '{account_type}' rate, but no matching discount is "
            f"defined in the price list. The quote uses standard prices."
            + (f" Rates found in the price list: {available}." if available else "")
        )
    return order, line, notes


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
    # Search every pinned price list, not just the first — a skill may pin more than one.
    files = list(source_files) if source_files else [""]
    source_file = files[0]
    detected_currency = ""

    def _find(finder, query: str) -> Optional[dict]:
        for f in files:
            hit = finder(query, f)
            if hit:
                return hit
        return None

    for item in identified.items:
        db_row: Optional[dict] = None

        # 1. Exact SKU lookup
        if item.sku:
            db_row = _find(search_by_sku, item.sku)

        # 2. FTS5 trigram search by full description
        if db_row is None:
            db_row = _find(lambda q, f: (search_items(q, f, limit=3) or [None])[0], item.description)

        # 3. Shorter query (first 3 significant words) as fallback
        if db_row is None:
            words = [w for w in item.description.split() if len(w) > 2][:3]
            short_query = " ".join(words)
            if short_query and short_query.lower() != item.description.lower():
                db_row = _find(lambda q, f: (search_items(q, f, limit=3) or [None])[0], short_query)

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

        if not detected_currency and db_row.get("currency"):
            detected_currency = db_row["currency"]

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

    # Discounts come from rates stated in the price list, never from the model.
    order_discounts, line_discounts, notes = build_order_discounts(
        identified.account_type, source_file
    )
    warnings.extend(notes)
    for spec in line_discounts:
        for li in line_items:
            li.discount_percent = spec.rate_percent
            li.discount_reason = spec.description

    return (
        ExtractedData(
            line_items=line_items,
            order_discounts=order_discounts,
            customer=identified.customer,
            metadata=identified.metadata,
            currency=detected_currency,
        ),
        warnings,
    )
