"""Field schemas and validation, including the checks that catch what OCR cannot.

Extraction confidence tells you how sure the *reader* was. Validation tells you whether
the answer is possible. Those are different questions, and the second one is the useful
one:

    line items: 12,000 + 8,500 + 3,200   =  23,700
    stated total:                           27,300

Every field here might have been read with 99% confidence. The document is still wrong —
either the OCR dropped a line, or the invoice does not add up. **Arithmetic consistency
catches errors that no per-character confidence ever will**, and it is the cheapest
quality signal in document processing.

Three layers, cheapest first:

  **type**        does the text parse as the declared type at all
  **format**      does it satisfy the format rules for that field (CNIC checksum shape,
                  NTN length, date validity)
  **cross-field** do the fields agree with each other
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Pakistani identifiers, because a document-processing system built elsewhere will not
# know them and they appear on every invoice and contract here.
CNIC = re.compile(r"^\d{5}-\d{7}-\d$")
NTN = re.compile(r"^\d{7}(-\d)?$")  # National Tax Number
STRN = re.compile(r"^\d{2}-\d{2}-\d{4}-\d{3}-\d{2}$")  # Sales Tax Registration
IBAN_PK = re.compile(r"^PK\d{2}[A-Z]{4}\d{16}$")
PHONE_PK = re.compile(r"^(?:\+92|0)3\d{2}-?\d{7}$")


class FieldType:
    STRING = "string"
    NUMBER = "number"
    MONEY = "money"
    DATE = "date"
    CNIC = "cnic"
    NTN = "ntn"
    STRN = "strn"
    IBAN = "iban"
    PHONE = "phone"


@dataclass
class FieldSpec:
    name: str
    type: str = FieldType.STRING
    required: bool = True
    # A field nobody will act on does not need to stop the document.
    critical: bool = False
    description: str = ""


@dataclass
class Issue:
    field: str
    kind: str  # "missing" | "type" | "format" | "cross_field"
    detail: str
    severity: str  # "error" | "warning"


# Matches the first number in a string, with optional thousands separators and decimals.
_AMOUNT = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_money(raw: str) -> Decimal | None:
    """Parse an amount, tolerating the separators that appear in scanned documents.

    The number is **matched**, not stripped down to. Deleting every non-digit character
    keeps the full stop in "Rs." and produces "..99.50", which then fails to parse — and
    "Rs." is how amounts are written on most invoices here, so that is not an edge case.

    Returns None rather than raising: an unparseable amount is an extraction problem to
    be routed to a human, not an exception to crash the pipeline on.
    """
    if raw is None:
        return None
    match = _AMOUNT.search(str(raw))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def parse_date(raw: str) -> dt.date | None:
    """Parse a date, preferring day-first.

    Pakistan writes DD/MM/YYYY. Defaulting to month-first would read 03/04/2026 as
    3 April in one system and 4 March in another, and silently wrong dates on contracts
    and invoices are expensive in a way that is very hard to trace back.
    """
    if not raw:
        return None
    text = str(raw).strip()
    for pattern in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
    ):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


# Public: other modules (extract.py's _format_ok) key off this too, so it isn't
# private to this module even though it started that way.
FIELD_FORMATS: dict[str, tuple[re.Pattern, str]] = {
    FieldType.CNIC: (CNIC, "expected 00000-0000000-0"),
    FieldType.NTN: (NTN, "expected 7 digits, optionally with a check digit"),
    FieldType.STRN: (STRN, "expected 00-00-0000-000-00"),
    FieldType.IBAN: (IBAN_PK, "expected PK00XXXX0000000000000000"),
    FieldType.PHONE: (PHONE_PK, "expected 03XX-XXXXXXX or +923XXXXXXXXX"),
}


def validate_field(spec: FieldSpec, value) -> list[Issue]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if spec.required:
            return [
                Issue(
                    spec.name,
                    "missing",
                    "required field not found",
                    "error" if spec.critical else "warning",
                )
            ]
        return []

    severity = "error" if spec.critical else "warning"

    if spec.type in (FieldType.NUMBER, FieldType.MONEY):
        if parse_money(value) is None:
            return [Issue(spec.name, "type", f"{value!r} is not a number", severity)]
        return []

    if spec.type == FieldType.DATE:
        if parse_date(value) is None:
            return [Issue(spec.name, "type", f"{value!r} is not a recognised date", severity)]
        return []

    if spec.type in FIELD_FORMATS:
        pattern, expectation = FIELD_FORMATS[spec.type]
        if not pattern.match(str(value).strip()):
            return [Issue(spec.name, "format", f"{value!r}: {expectation}", severity)]

    return []


# --- cross-field rules ------------------------------------------------------------

CrossFieldRule = Callable[[dict], list[Issue]]


def line_items_sum_to_subtotal(tolerance: Decimal = Decimal("0.01")) -> CrossFieldRule:
    """The single most valuable check in invoice processing.

    Catches a dropped line, a misread digit and a fabricated total — none of which
    per-character confidence can see, because each individual read was fine.
    """

    def rule(fields: dict) -> list[Issue]:
        items = fields.get("line_items")
        subtotal = parse_money(fields.get("subtotal"))
        if not items or subtotal is None:
            return []
        if not all(isinstance(i, dict) for i in items):
            # A table-extraction step handed us rows that aren't `{"amount": ...}`
            # mappings - can't sum them, but that's a reason to ask a human, not a
            # reason to crash the caller's request.
            return [
                Issue(
                    "line_items",
                    "type",
                    f"line_items must be a list of {{'amount': ...}} objects; got {items!r}",
                    "error",
                )
            ]
        total = sum((parse_money(i.get("amount")) or Decimal(0)) for i in items)
        if abs(total - subtotal) > tolerance:
            return [
                Issue(
                    "subtotal",
                    "cross_field",
                    f"line items sum to {total} but subtotal states {subtotal} "
                    f"(difference {total - subtotal})",
                    "error",
                )
            ]
        return []

    return rule


def totals_are_consistent(tolerance: Decimal = Decimal("0.01")) -> CrossFieldRule:
    """subtotal + tax − discount = total."""

    def rule(fields: dict) -> list[Issue]:
        subtotal = parse_money(fields.get("subtotal"))
        tax = parse_money(fields.get("tax")) or Decimal(0)
        discount = parse_money(fields.get("discount")) or Decimal(0)
        total = parse_money(fields.get("total"))
        if subtotal is None or total is None:
            return []
        expected = subtotal + tax - discount
        if abs(expected - total) > tolerance:
            return [
                Issue(
                    "total",
                    "cross_field",
                    f"subtotal {subtotal} + tax {tax} - discount {discount} = {expected}, "
                    f"but total states {total}",
                    "error",
                )
            ]
        return []

    return rule


def date_order(earlier: str, later: str) -> CrossFieldRule:
    """A due date before its invoice date is a misread year, every time."""

    def rule(fields: dict) -> list[Issue]:
        a, b = parse_date(fields.get(earlier)), parse_date(fields.get(later))
        if a is None or b is None:
            return []
        if b < a:
            return [Issue(later, "cross_field", f"{later} ({b}) precedes {earlier} ({a})", "error")]
        return []

    return rule


def tax_rate_is_plausible(
    rates: Sequence[Decimal] = (Decimal("0"), Decimal("0.17"), Decimal("0.18")),
) -> CrossFieldRule:
    """Sales tax in Pakistan sits at a small set of statutory rates.

    An implied rate of 1.7% or 170% is a decimal point in the wrong place — which is the
    single most common numeric OCR error and the one least likely to look wrong.
    """

    def rule(fields: dict) -> list[Issue]:
        subtotal = parse_money(fields.get("subtotal"))
        tax = parse_money(fields.get("tax"))
        if not subtotal or tax is None:
            return []
        implied = tax / subtotal
        if any(abs(implied - r) < Decimal("0.005") for r in rates):
            return []
        return [
            Issue(
                "tax",
                "cross_field",
                f"implied tax rate {implied:.1%} is not a standard rate",
                "warning",
            )
        ]

    return rule


@dataclass
class DocumentSchema:
    name: str
    fields: list[FieldSpec] = field(default_factory=list)
    cross_field_rules: list[CrossFieldRule] = field(default_factory=list)

    def spec(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def validate(self, extracted: dict) -> list[Issue]:
        issues: list[Issue] = []
        for spec in self.fields:
            issues.extend(validate_field(spec, extracted.get(spec.name)))
        for rule in self.cross_field_rules:
            issues.extend(rule(extracted))
        return issues


INVOICE = DocumentSchema(
    name="invoice",
    fields=[
        FieldSpec("invoice_number", FieldType.STRING, critical=True),
        FieldSpec("invoice_date", FieldType.DATE, critical=True),
        FieldSpec("due_date", FieldType.DATE, required=False),
        FieldSpec("vendor_name", FieldType.STRING, critical=True),
        FieldSpec("vendor_ntn", FieldType.NTN, required=False),
        FieldSpec("vendor_strn", FieldType.STRN, required=False),
        FieldSpec("subtotal", FieldType.MONEY, critical=True),
        FieldSpec("tax", FieldType.MONEY, required=False),
        FieldSpec("discount", FieldType.MONEY, required=False),
        FieldSpec("total", FieldType.MONEY, critical=True),
        FieldSpec("iban", FieldType.IBAN, required=False),
    ],
    cross_field_rules=[
        line_items_sum_to_subtotal(),
        totals_are_consistent(),
        date_order("invoice_date", "due_date"),
        tax_rate_is_plausible(),
    ],
)

CONTRACT = DocumentSchema(
    name="contract",
    fields=[
        FieldSpec("party_a", FieldType.STRING, critical=True),
        FieldSpec("party_b", FieldType.STRING, critical=True),
        FieldSpec("effective_date", FieldType.DATE, critical=True),
        FieldSpec("expiry_date", FieldType.DATE, required=False),
        FieldSpec("value", FieldType.MONEY, required=False),
        FieldSpec("governing_law", FieldType.STRING, required=False),
    ],
    cross_field_rules=[date_order("effective_date", "expiry_date")],
)

CNIC_DOC = DocumentSchema(
    name="cnic",
    fields=[
        FieldSpec("name", FieldType.STRING, critical=True),
        FieldSpec("cnic_number", FieldType.CNIC, critical=True),
        FieldSpec("date_of_birth", FieldType.DATE, required=False),
        FieldSpec("date_of_expiry", FieldType.DATE, required=False),
    ],
    cross_field_rules=[date_order("date_of_birth", "date_of_expiry")],
)

SCHEMAS = {s.name: s for s in (INVOICE, CONTRACT, CNIC_DOC)}
