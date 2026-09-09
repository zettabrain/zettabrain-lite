"""Unit tests for money formatting, order discounts, and the computed-data block."""

from decimal import Decimal

from zettabrain_lite.generation.pipeline import (
    ComputedResult,
    ExtractedData,
    LineItem,
    OrderDiscount,
    TaxSpec,
    build_computed_summary,
    compute_totals,
    money,
    qty,
)


class TestMoney:
    def test_thousands_separator_and_two_places(self):
        assert money(Decimal("420000"), "NGN") == "₦420,000.00"
        assert money(1548500, "NGN") == "₦1,548,500.00"

    def test_float_from_sqlite_loses_trailing_zero(self):
        """base_price is a SQLite REAL, so str() alone produced '420000.0'."""
        assert money(420000.0, "NGN") == "₦420,000.00"

    def test_unknown_currency_uses_iso_code(self):
        assert money(1000, "XYZ") == "XYZ 1,000.00"

    def test_no_currency(self):
        assert money(1000) == "1,000.00"

    def test_non_numeric_passes_through(self):
        assert money("[NEEDS INPUT]", "NGN") == "[NEEDS INPUT]"


class TestQty:
    def test_trailing_zeros_dropped(self):
        assert qty(Decimal("3.0")) == "3"
        assert qty(Decimal("2.50")) == "2.5"
        assert qty(7.5) == "7.5"


def _items():
    return [
        LineItem(description="Executive Office Package", unit="Per month",
                 quantity=Decimal("3"), unit_price=Decimal("420000")),
        LineItem(description="Reception Welcome Arrangement", unit="Per unit",
                 quantity=Decimal("2"), unit_price=Decimal("140000")),
    ]


class TestOrderDiscounts:
    def test_applies_above_threshold(self):
        data = ExtractedData(
            line_items=_items(), currency="NGN",
            order_discounts=[OrderDiscount(description="Bulk order discount",
                                           rate_percent=Decimal("10"),
                                           threshold=Decimal("500000"))],
        )
        result = compute_totals(data)
        assert result.order_discount_details[0]["amount"] == "154000.00"
        assert result.grand_total == Decimal("1386000.00")

    def test_skipped_below_threshold_and_says_why(self):
        data = ExtractedData(
            line_items=[LineItem(description="Card", quantity=Decimal("1"),
                                 unit_price=Decimal("3500"))],
            currency="NGN",
            order_discounts=[OrderDiscount(description="Bulk order discount",
                                           rate_percent=Decimal("10"),
                                           threshold=Decimal("500000"))],
        )
        result = compute_totals(data)
        assert result.order_discount_details == []
        assert any("below the" in line for line in result.computation_log)
        assert result.grand_total == Decimal("3500.00")

    def test_line_and_order_discounts_compose(self):
        items = _items()
        for li in items:
            li.discount_percent = Decimal("12")
            li.discount_reason = "Corporate account discount"
        data = ExtractedData(
            line_items=items, currency="NGN",
            order_discounts=[OrderDiscount(description="Bulk", rate_percent=Decimal("10"),
                                           threshold=Decimal("500000"))],
            taxes=[TaxSpec(description="VAT", rate_percent=Decimal("7.5"))],
        )
        result = compute_totals(data)
        # 1,540,000 - 12% = 1,355,200; -10% = 1,219,680; +7.5% VAT
        assert result.discounted_subtotal == Decimal("1219680.00")
        assert result.grand_total == Decimal("1311156.00")

    def test_no_threshold_always_applies(self):
        data = ExtractedData(
            line_items=[LineItem(description="Card", quantity=Decimal("1"),
                                 unit_price=Decimal("1000"))],
            currency="NGN",
            order_discounts=[OrderDiscount(description="Launch offer",
                                           rate_percent=Decimal("5"))],
        )
        result = compute_totals(data)
        assert result.order_discount_details[0]["amount"] == "50.00"


class TestComputedSummary:
    """The block handed to the model must not read as a finished document.

    A markdown version of this block was reproduced verbatim into a customer's quote by a
    3.8B model, internal calculation log included.
    """

    def _summary(self):
        data = ExtractedData(line_items=_items(), currency="NGN",
                             taxes=[TaxSpec(description="VAT", rate_percent=Decimal("7.5"))],
                             customer={"name": "Ardova Energy Plc"})
        return build_computed_summary(compute_totals(data))

    def test_has_no_markdown_headings(self):
        assert "##" not in self._summary()

    def test_has_no_markdown_table(self):
        assert "|---" not in self._summary()

    def test_omits_the_internal_calculation_log(self):
        summary = self._summary()
        assert "Calculation Summary" not in summary
        assert "Product subtotal:" not in summary

    def test_carries_the_values(self):
        summary = self._summary()
        assert "₦420,000.00" in summary
        assert "GRAND_TOTAL" in summary
        assert "Ardova Energy Plc" in summary

    def test_amounts_are_formatted(self):
        assert "420000.0" not in self._summary()


class TestComputedResultDefaults:
    def test_order_discount_details_defaults_empty(self):
        """Existing callers construct ComputedResult without the new field."""
        result = ComputedResult(
            line_details=[], fee_details=[], tax_details=[],
            product_subtotal=Decimal("0"), total_discounts=Decimal("0"),
            discounted_subtotal=Decimal("0"), total_fees=Decimal("0"),
            subtotal_before_tax=Decimal("0"), total_tax=Decimal("0"),
            grand_total=Decimal("0"), computation_log=[], customer={}, metadata={},
        )
        assert result.order_discount_details == []


class TestPromptBudget:
    """Small local models have small context windows, and Ollama truncates silently.

    A real run on a 2GB-context model logged: truncating input prompt limit=2050
    prompt=6291 — two thirds of the instructions were discarded without an error.
    """

    def test_estimate_tokens(self):
        from zettabrain_lite.generation.pipeline import estimate_tokens

        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("") == 0

    def test_short_corpus_is_untouched(self):
        from zettabrain_lite.generation.pipeline import trim_corpus

        text = "Terms and conditions apply."
        assert trim_corpus(text, 2000) == text

    def test_long_corpus_is_trimmed_and_says_so(self):
        from zettabrain_lite.generation.pipeline import trim_corpus

        text = "\n".join(f"line {i} of source material" for i in range(500))
        trimmed = trim_corpus(text, 500)
        assert len(trimmed) < 700
        assert "omitted to fit" in trimmed

    def test_trim_cuts_at_a_line_boundary(self):
        from zettabrain_lite.generation.pipeline import trim_corpus

        text = "\n".join(f"line {i}" for i in range(200))
        body = trim_corpus(text, 400).split("\n\n[")[0]
        assert not body.endswith("lin")

    def test_empty_corpus(self):
        from zettabrain_lite.generation.pipeline import trim_corpus

        assert trim_corpus("", 100) == ""

    def test_format_prompt_respects_the_cap(self):
        from zettabrain_lite.generation.pipeline import build_format_prompt, estimate_tokens

        data = ExtractedData(line_items=_items(), currency="NGN")
        computed = compute_totals(data)
        corpus = "source line\n" * 4000
        uncapped = build_format_prompt("do the thing", corpus, "quote please", computed)
        capped = build_format_prompt("do the thing", corpus, "quote please", computed,
                                     max_corpus_chars=2000)
        assert estimate_tokens(capped) < estimate_tokens(uncapped) / 2


class TestLineTotalLabelling:
    """Gross and net must both be named.

    Emitting only the net under the label "line_total" produced customer-facing tables where
    quantity x unit price did not equal the stated line total.
    """

    def _summary_with_discount(self):
        items = _items()
        for li in items:
            li.discount_percent = Decimal("12")
            li.discount_reason = "Corporate account discount"
        return build_computed_summary(compute_totals(
            ExtractedData(line_items=items, currency="NGN")
        ))

    def test_gross_line_total_is_present(self):
        summary = self._summary_with_discount()
        assert "₦1,260,000.00" in summary  # 3 x 420,000

    def test_net_is_named_separately(self):
        summary = self._summary_with_discount()
        assert "net_total" in summary
        assert "₦1,108,800.00" in summary  # after 12%

    def test_gross_is_labelled_as_the_product(self):
        assert "(quantity x unit_price)" in self._summary_with_discount()

    def test_no_net_line_when_there_is_no_discount(self):
        summary = build_computed_summary(compute_totals(
            ExtractedData(line_items=_items(), currency="NGN")
        ))
        assert "net_total" not in summary
        assert "₦1,260,000.00" in summary
