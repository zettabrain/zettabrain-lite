"""Rate and column detection must work for any trade, not only the one it was built against.

The cases below come from three unrelated price lists — a plumber's rate card, a tyre shop's
stock list and a construction schedule — which differ in header position, column wording,
currency and file format. Nothing here is specific to one industry.
"""

import pytest

from zettabrain_lite.generation.pipeline import _label_words
from zettabrain_lite.price_list import (
    _TAX_NAME_RE,
    classify_setting,
    currency_from_header,
    detect_columns,
    setting_from_pair,
)

# ── Label/value classification ────────────────────────────────────────────────


class TestSettingClassification:
    """Rate or amount is decided by the value, not only by the label wording."""

    def test_fractional_rate_is_a_percentage(self):
        s = setting_from_pair("VAT rate", 0.075)
        assert s["kind"] == "tax"
        assert s["percent"] == 7.5

    def test_written_percentage(self):
        s = setting_from_pair("Sales tax rate", "7%")
        assert s["kind"] == "tax"
        assert s["percent"] == 7.0

    def test_label_naming_both_discount_and_threshold_reads_as_threshold(self):
        """'Volume discount threshold = 2000' is an amount; 2000% is nonsense."""
        s = setting_from_pair("Volume discount threshold", 2000)
        assert s["kind"] == "threshold"
        assert s["amount"] == 2000

    def test_discount_qualified_by_threshold_wording_stays_a_rate(self):
        s = setting_from_pair("Bulk order discount (orders above threshold)", 0.1)
        assert s["kind"] == "discount"
        assert s["percent"] == 10.0

    def test_absolute_threshold(self):
        s = setting_from_pair("Bulk order threshold (order value)", 500000)
        assert s["kind"] == "threshold"
        assert s["amount"] == 500000

    def test_surcharge(self):
        assert setting_from_pair("Out of hours surcharge", 0.5)["kind"] == "surcharge"

    def test_trade_and_fleet_discounts(self):
        assert setting_from_pair("Trade account discount", 0.1)["kind"] == "discount"
        assert setting_from_pair("Fleet account discount", 0.08)["percent"] == 8.0

    def test_unrelated_label_ignored(self):
        assert setting_from_pair("Price list effective date", 0.5) is None

    def test_formula_without_cached_value_ignored(self):
        assert setting_from_pair("VAT rate", "=Assumptions!B6") is None

    def test_classify_setting_returns_empty_for_plain_labels(self):
        assert classify_setting("Product name") == ""


# ── Discount / threshold pairing ──────────────────────────────────────────────


class TestLabelPairing:
    """A discount pairs with its threshold by identity, not by the word 'discount'."""

    def test_identifying_words_exclude_generic_rate_words(self):
        assert _label_words("Fleet account discount") == {"fleet"}
        assert _label_words("Volume discount threshold") == {"volume"}
        assert _label_words("Bulk order discount (orders above threshold)") == {"bulk"}

    def test_unrelated_discount_does_not_pair_with_a_threshold(self):
        assert not (_label_words("Fleet account discount")
                    & _label_words("Volume discount threshold"))

    def test_matching_discount_pairs_with_its_threshold(self):
        assert _label_words("Volume discount") & _label_words("Volume discount threshold")


# ── Tax naming ────────────────────────────────────────────────────────────────


class TestTaxNaming:
    def test_multiword_name_is_kept(self):
        assert _TAX_NAME_RE.search("Sales tax rate").group(1).lower() == "sales tax"

    def test_acronym_is_matched(self):
        assert _TAX_NAME_RE.search("VAT rate applied to all items").group(1).lower() == "vat"

    def test_gst(self):
        assert _TAX_NAME_RE.search("GST rate").group(1).lower() == "gst"


# ── Column and currency detection across trades ───────────────────────────────


class TestColumnDetectionAcrossTrades:
    @pytest.mark.parametrize(
        "headers,expected_price_header",
        [
            (["Service Code", "Work Item", "Detail", "Charged Per", "Rate (GBP)"], "Rate (GBP)"),
            (["Part No", "Product", "Size", "Unit", "Unit Cost USD"], "Unit Cost USD"),
            (["Item Ref", "Description", "Unit of Measure", "Rate (NGN)", "Notes"], "Rate (NGN)"),
            (["SKU", "Item", "UOM", "Price"], "Price"),
        ],
    )
    def test_price_column_found(self, headers, expected_price_header):
        col_map = detect_columns(headers)
        assert col_map, f"no columns detected for {headers}"
        assert headers[col_map["price"]] == expected_price_header

    @pytest.mark.parametrize(
        "header,code",
        [
            ("Rate (GBP)", "GBP"),
            ("Unit Cost USD", "USD"),
            ("Rate (NGN)", "NGN"),
            ("Price (₹)", "INR"),
            ("Amount", ""),
        ],
    )
    def test_currency_from_header(self, header, code):
        assert currency_from_header(header) == code
