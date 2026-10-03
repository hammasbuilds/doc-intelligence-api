"""Amounts as they are actually printed on Pakistani / South Asian invoices.

Regression for round-2 review: `Rs. 11,800.00` was not extracted at all, and the tax
field took the "18" from "GST 18%" instead of the amount, so the subtotal + tax = total
check never ran on a real-looking invoice.
"""

from __future__ import annotations

import pytest

from docintel.extract import FIELD_EXTRACTORS, extract_document
from docintel.pipeline import process

PK_INVOICE = """TAX INVOICE
Invoice No: INV-1001
Invoice Date: 02/10/2026
Seller: Indus Traders (Pvt) Ltd
Bill To: Acme Foods
NTN: 1234567
Subtotal: Rs. 10,000.00
GST 18%: Rs. 1,800.00
Total: Rs. 11,800.00
"""


def _first(field: str, line: str) -> str | None:
    found = FIELD_EXTRACTORS["invoice"][field][0](line)
    return found[0] if found else None


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Total: Rs. 11,800.00", "11,800.00"),
        ("Total: Rs 11,800", "11,800"),
        ("Total: PKR 11,800.00", "11,800.00"),
        ("Total PKR. 11800", "11800"),
        ("Grand Total: \u20a8 11,800.50", "11,800.50"),
        ("Total: Rs.11,800.00", "11,800.00"),
        ("Total: 1,18,000.00", "1,18,000.00"),  # lakh grouping
        ("Total: Rs. 12,34,567", "12,34,567"),
        ("Amount Payable: Rs. 500", "500"),
        ("Total: 27,729.00", "27,729.00"),  # bare number still works
    ],
)
def test_total_accepts_currency_prefixes_and_grouping(line, expected):
    assert _first("total", line) == expected


@pytest.mark.parametrize(
    "line, expected",
    [
        ("GST 18%: Rs. 1,800.00", "1,800.00"),
        ("Sales Tax @ 17%: 1,700.00", "1,700.00"),
        ("Sales Tax (18%) PKR 1,800", "1,800"),
        ("GST 17.5 %: Rs 1,750.00", "1,750.00"),
        ("Sales Tax: 4,029.00", "4,029.00"),
    ],
)
def test_tax_takes_the_amount_not_the_rate(line, expected):
    assert _first("tax", line) == expected


@pytest.mark.parametrize("line", ["GST 18%", "Sales Tax @ 18%\n1,800.00", "GST: 18%"])
def test_a_rate_alone_is_never_the_tax_amount(line):
    assert _first("tax", line) is None


def test_heading_does_not_borrow_next_line_amount():
    assert _first("tax", "TAX INVOICE\n1,234.00") is None


def test_registration_number_is_not_an_amount():
    assert _first("tax", "Sales Tax Reg No: 03-02-9999-123-45") is None


def test_realistic_pk_invoice_extracts_every_amount_and_vendor():
    values = {k: f.value for k, f in extract_document(PK_INVOICE, "invoice").items()}
    assert values["subtotal"] == "10,000.00"
    assert values["tax"] == "1,800.00"
    assert values["total"] == "11,800.00"
    assert values["vendor_name"] == "Indus Traders (Pvt) Ltd"


def test_correct_pk_invoice_goes_straight_through():
    doc = process(PK_INVOICE)
    assert doc.auto_approved, doc.summary()


def test_wrong_total_on_pk_invoice_is_caught_by_arithmetic():
    doc = process(PK_INVOICE.replace("Rs. 11,800.00", "Rs. 12,800.00"))
    assert not doc.auto_approved
    assert any(i.kind == "cross_field" and i.field == "total" for i in doc.issues)


def test_bill_to_is_the_buyer_not_the_vendor():
    text = "TAX INVOICE\nBill To: Acme Foods\nSubtotal: 100\n"
    assert extract_document(text, "invoice")["vendor_name"].value is None
