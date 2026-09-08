"""Unit tests for price list column detection and currency parsing."""

from zettabrain_lite.price_list import currency_from_header, detect_columns, parse_price

# The header row of a real price list, which sat at row 6 under a title block.
REAL_HEADERS = [
    "S/N", "Item Code", "Category", "Item or Service", "Description", "Unit",
    "Base Price (NGN)", "VAT (NGN)", "Price incl. VAT (NGN)", "Trade Price (NGN)",
    "Corporate Price (NGN)",
]

TITLE_ROW = ["Petals & Stems Florals Ltd.", None, None, None, None, None]


class TestDetectColumns:
    def test_real_header_row_resolves(self):
        col_map = detect_columns([str(h) for h in REAL_HEADERS])
        assert col_map["sku"] == 1
        assert col_map["category"] == 2
        assert col_map["name"] == 3
        assert col_map["unit"] == 5

    def test_base_price_wins_over_derived_price_columns(self):
        """A sheet with VAT/Trade/Corporate columns must still price off the base column."""
        col_map = detect_columns([str(h) for h in REAL_HEADERS])
        assert col_map["price"] == 6
        assert REAL_HEADERS[col_map["price"]] == "Base Price (NGN)"

    def test_title_row_yields_nothing(self):
        assert detect_columns([str(h) if h else "" for h in TITLE_ROW]) == {}

    def test_missing_price_column_yields_nothing(self):
        assert detect_columns(["Item", "Description", "Notes"]) == {}


class TestCurrencyFromHeader:
    def test_iso_code_in_header(self):
        assert currency_from_header("Base Price (NGN)") == "NGN"
        assert currency_from_header("Amount USD") == "USD"

    def test_symbol_in_header(self):
        assert currency_from_header("Price (₦)") == "NGN"
        assert currency_from_header("Cost £") == "GBP"

    def test_no_currency(self):
        assert currency_from_header("Base Price") == ""
        assert currency_from_header("") == ""

    def test_bare_r_is_not_treated_as_rand(self):
        """'R' appears in ordinary English headers and must not imply ZAR."""
        assert currency_from_header("Rate") == ""


class TestParsePrice:
    def test_plain_number(self):
        assert parse_price(45000) == (45000.0, "")

    def test_symbol_prefixed(self):
        assert parse_price("₦45,000") == (45000.0, "NGN")

    def test_iso_prefixed(self):
        assert parse_price("NGN 45000") == (45000.0, "NGN")

    def test_placeholder_values_rejected(self):
        for value in ("", "N/A", "TBC", "quote", "-"):
            assert parse_price(value) == (None, "")
