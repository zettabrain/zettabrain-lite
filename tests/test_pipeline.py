"""Unit tests for the Extract-Compute-Format pipeline."""

from decimal import Decimal

from zettabrain_lite.generation.pipeline import (
    ExtractedData,
    FeeItem,
    LineItem,
    TaxSpec,
    compute_totals,
    parse_extraction,
    validate_against_corpus,
)

# ── parse_extraction ──────────────────────────────────────────────────────────


class TestParseExtraction:
    def test_valid_json(self):
        raw = '{"line_items": [{"description": "Widget", "quantity": 10, "unit_price": 25.00}], "fees": [], "taxes": []}'
        result = parse_extraction(raw)
        assert result is not None
        assert len(result.line_items) == 1
        assert result.line_items[0].unit_price == Decimal("25.00")

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"line_items": [{"description": "Widget", "quantity": 5, "unit_price": 10}]}\n```'
        result = parse_extraction(raw)
        assert result is not None
        assert result.line_items[0].quantity == Decimal("5")

    def test_json_with_text_prefix(self):
        raw = 'Here is the extracted data:\n{"line_items": [{"description": "Item", "quantity": 1, "unit_price": 50}]}'
        result = parse_extraction(raw)
        assert result is not None

    def test_invalid_json(self):
        assert parse_extraction("not json at all") is None

    def test_malformed_json(self):
        assert parse_extraction('{"line_items": [}') is None

    def test_wrong_type_array(self):
        assert parse_extraction('[1, 2, 3]') is None

    def test_empty_line_items_returns_none(self):
        raw = '{"line_items": [], "fees": [], "taxes": []}'
        assert parse_extraction(raw) is None

    def test_missing_fields_use_defaults(self):
        raw = '{"line_items": [{"description": "X", "quantity": 1, "unit_price": 10}]}'
        result = parse_extraction(raw)
        assert result is not None
        assert result.fees == []
        assert result.taxes == []
        assert result.customer == {}

    def test_empty_string(self):
        assert parse_extraction("") is None


# ── compute_totals ────────────────────────────────────────────────────────────


class TestComputeTotals:
    def test_single_item(self):
        data = ExtractedData(line_items=[LineItem(description="Widget", quantity=Decimal("10"), unit_price=Decimal("25"))])
        result = compute_totals(data)
        assert result.product_subtotal == Decimal("250.00")
        assert result.grand_total == Decimal("250.00")

    def test_multiple_items(self):
        data = ExtractedData(
            line_items=[
                LineItem(description="A", quantity=Decimal("5"), unit_price=Decimal("10")),
                LineItem(description="B", quantity=Decimal("3"), unit_price=Decimal("20")),
            ]
        )
        result = compute_totals(data)
        assert result.product_subtotal == Decimal("110.00")
        assert result.grand_total == Decimal("110.00")

    def test_with_discount(self):
        data = ExtractedData(
            line_items=[
                LineItem(
                    description="R-22 Reclaimed",
                    quantity=Decimal("100"),
                    unit_price=Decimal("65"),
                    discount_percent=Decimal("5"),
                    discount_reason="100-249 lbs volume discount",
                )
            ]
        )
        result = compute_totals(data)
        assert result.product_subtotal == Decimal("6500.00")
        assert result.total_discounts == Decimal("325.00")
        assert result.discounted_subtotal == Decimal("6175.00")

    def test_with_fees(self):
        data = ExtractedData(
            line_items=[LineItem(description="Item", quantity=Decimal("1"), unit_price=Decimal("100"))],
            fees=[
                FeeItem(description="Delivery", amount=Decimal("45")),
                FeeItem(description="Environmental", amount=Decimal("5")),
            ],
        )
        result = compute_totals(data)
        assert result.total_fees == Decimal("50.00")
        assert result.subtotal_before_tax == Decimal("150.00")

    def test_with_tax(self):
        data = ExtractedData(
            line_items=[LineItem(description="Item", quantity=Decimal("1"), unit_price=Decimal("100"))],
            taxes=[TaxSpec(description="Sales Tax", rate_percent=Decimal("5.3"))],
        )
        result = compute_totals(data)
        assert result.total_tax == Decimal("5.30")
        assert result.grand_total == Decimal("105.30")

    def test_full_3rva_scenario(self):
        """100 lbs R-22 Reclaimed, Zone 2 (Glen Allen), Emergency delivery."""
        data = ExtractedData(
            line_items=[
                LineItem(
                    description="R-22 Refrigerant - Reclaimed ARI-700 Certified",
                    unit="lb",
                    quantity=Decimal("100"),
                    unit_price=Decimal("65"),
                    discount_percent=Decimal("5"),
                    discount_reason="100-249 lbs: 5% off product price",
                    source_ref="3rva-pricing-rules",
                )
            ],
            fees=[
                FeeItem(description="Zone 2 Delivery (Glen Allen)", amount=Decimal("45"), source_ref="3rva-pricing-rules"),
                FeeItem(description="Emergency Delivery (2-4 hours)", amount=Decimal("150"), source_ref="3rva-pricing-rules"),
                FeeItem(description="Environmental Fee (2 cylinders)", amount=Decimal("5"), source_ref="3rva-pricing-rules"),
            ],
            taxes=[
                TaxSpec(description="Virginia Sales Tax", rate_percent=Decimal("5.3"), source_ref="3rva-pricing-rules"),
            ],
            customer={
                "name": "Premier HVAC Services LLC",
                "contact": "John Martinez",
                "phone": "(804) 555-1234",
                "address": "4500 Cox Road, Glen Allen, VA 23060",
            },
            metadata={
                "delivery_address": "4500 Cox Road, Glen Allen, VA 23060",
                "delivery_speed": "emergency",
                "delivery_zone": "Zone 2",
            },
        )
        result = compute_totals(data)

        assert result.product_subtotal == Decimal("6500.00")
        assert result.total_discounts == Decimal("325.00")
        assert result.discounted_subtotal == Decimal("6175.00")
        assert result.total_fees == Decimal("200.00")
        assert result.subtotal_before_tax == Decimal("6375.00")
        assert result.total_tax == Decimal("337.88")
        assert result.grand_total == Decimal("6712.88")

    def test_rounding(self):
        """Verify rounding to 2 decimal places."""
        data = ExtractedData(
            line_items=[LineItem(description="Item", quantity=Decimal("3"), unit_price=Decimal("10.33"))],
            taxes=[TaxSpec(description="Tax", rate_percent=Decimal("7.25"))],
        )
        result = compute_totals(data)
        assert result.product_subtotal == Decimal("30.99")
        assert result.total_tax == Decimal("2.25")
        assert result.grand_total == Decimal("33.24")

    def test_zero_discount(self):
        data = ExtractedData(
            line_items=[LineItem(description="Item", quantity=Decimal("1"), unit_price=Decimal("50"), discount_percent=Decimal("0"))],
        )
        result = compute_totals(data)
        assert result.total_discounts == Decimal("0.00")
        assert result.grand_total == Decimal("50.00")

    def test_computation_log_populated(self):
        data = ExtractedData(
            line_items=[LineItem(description="Widget", quantity=Decimal("2"), unit_price=Decimal("10"))],
        )
        result = compute_totals(data)
        assert len(result.computation_log) > 0
        assert any("GRAND TOTAL" in line for line in result.computation_log)


# ── validate_against_corpus ───────────────────────────────────────────────────


class TestValidateAgainstCorpus:
    def test_all_prices_found(self):
        corpus = "R-22 Reclaimed: $65.00/lb\nDelivery Zone 2: $45\nTax: 5.3%"
        data = ExtractedData(
            line_items=[LineItem(description="R-22", quantity=Decimal("100"), unit_price=Decimal("65.00"))],
            fees=[FeeItem(description="Delivery", amount=Decimal("45"))],
            taxes=[TaxSpec(description="Tax", rate_percent=Decimal("5.3"))],
        )
        warnings = validate_against_corpus(data, corpus)
        assert warnings == []

    def test_invented_price_triggers_warning(self):
        corpus = "R-22 Reclaimed: $65.00/lb"
        data = ExtractedData(
            line_items=[LineItem(description="R-22", quantity=Decimal("100"), unit_price=Decimal("99.99"))],
        )
        warnings = validate_against_corpus(data, corpus)
        assert len(warnings) == 1
        assert "99.99" in warnings[0]

    def test_missing_fee_triggers_warning(self):
        corpus = "Delivery: $45"
        data = ExtractedData(
            line_items=[LineItem(description="X", quantity=Decimal("1"), unit_price=Decimal("45"))],
            fees=[FeeItem(description="Rush", amount=Decimal("999"))],
        )
        warnings = validate_against_corpus(data, corpus)
        assert any("999" in w for w in warnings)

    def test_missing_tax_triggers_warning(self):
        corpus = "Tax rate: 5.3%"
        data = ExtractedData(
            line_items=[LineItem(description="X", quantity=Decimal("1"), unit_price=Decimal("10"))],
            taxes=[TaxSpec(description="Tax", rate_percent=Decimal("9.9"))],
        )
        warnings = validate_against_corpus(data, corpus)
        assert any("9.9" in w for w in warnings)
