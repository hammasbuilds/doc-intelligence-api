"""Classification and field extraction, with confidence that means something.

**The confidence problem.** An OCR engine's per-character probability tells you how
clearly the ink was printed. It tells you almost nothing about whether the *right* value
ended up in the right field — which is the thing a downstream system actually needs to
know. A model that reads `12,000` perfectly from the wrong column is 99% confident and
completely wrong.

So confidence here is built from things that can actually be checked:

  **agreement**   several independent extractors run per field; when they agree, that is
                  evidence, and when they disagree the field is uncertain regardless of
                  what any single one reported
  **format**      a value that satisfies its field's format is more likely to be the
                  right value
  **position**    a value found next to its expected label beats one found loose in the
                  page

None of these is a probability and none is presented as one. They combine into a score
used for **routing**, not for reporting certainty to a user.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .schema import FIELD_FORMATS, SCHEMAS, DocumentSchema, FieldType, parse_date, parse_money

# --- classification ---------------------------------------------------------------

CLASSIFIER_RULES: dict[str, list[str]] = {
    "invoice": [
        "invoice",
        "bill to",
        "subtotal",
        "sales tax",
        "amount due",
        "tax invoice",
        "purchase order",
    ],
    "contract": [
        "agreement",
        "hereinafter",
        "witnesseth",
        "governing law",
        "party of the first part",
        "in witness whereof",
        "this agreement",
    ],
    "cnic": [
        "national identity card",
        "nadra",
        "identity number",
        "father name",
        "date of expiry",
        "pakistan",
    ],
    "receipt": ["receipt", "paid", "cash", "change due", "thank you for"],
    "bank_statement": [
        "statement of account",
        "opening balance",
        "closing balance",
        "debit",
        "credit",
        "iban",
    ],
}


@dataclass
class Classification:
    document_type: str | None
    confidence: float
    matched: list[str] = field(default_factory=list)
    runner_up: str | None = None
    reason: str = ""

    @property
    def needs_human(self) -> bool:
        return self.document_type is None


def classify(text: str, *, floor: float = 0.45, margin: float = 0.15) -> Classification:
    """Identify the document type, or decline to.

    Two ways to decline, and both matter. A document matching nothing is unknown. A
    document matching two types almost equally is *ambiguous* — and ambiguity is the
    more dangerous case, because a forced choice there looks exactly like a confident
    correct answer. An invoice attached to a contract is a real document and a real
    problem.
    """
    lowered = text.lower()
    scores: dict[str, list[str]] = {}

    for doc_type, markers in CLASSIFIER_RULES.items():
        hits = [m for m in markers if m in lowered]
        if hits:
            scores[doc_type] = hits

    if not scores:
        return Classification(None, 0.0, reason="no document type matched")

    ranked = sorted(scores.items(), key=lambda kv: -len(kv[1]))
    total = sum(len(v) for v in scores.values())
    best, best_hits = ranked[0]
    confidence = round(len(best_hits) / total, 4)

    runner_up = ranked[1][0] if len(ranked) > 1 else None
    runner_up_confidence = round(len(ranked[1][1]) / total, 4) if runner_up else 0.0

    if confidence < floor:
        return Classification(
            None,
            confidence,
            best_hits,
            runner_up,
            reason=f"best match {best} scored only {confidence:.0%}",
        )

    if runner_up and confidence - runner_up_confidence < margin:
        return Classification(
            None,
            confidence,
            best_hits,
            runner_up,
            reason=f"ambiguous between {best} and {runner_up} "
            f"({confidence:.0%} vs {runner_up_confidence:.0%})",
        )

    return Classification(best, confidence, best_hits, runner_up)


# --- extraction -------------------------------------------------------------------

Extractor = Callable[[str], list[str]]


def labelled(*labels: str, pattern: str = r"[^\n]{1,60}") -> Extractor:
    """Find a value on the same line as one of its labels.

    Positional evidence: a number beside "Total:" is far more likely to be the total
    than the same number found loose on the page.

    The leading word boundary is load-bearing. Without it the label `total` matches
    inside **Sub**total, and an invoice's total is silently extracted as its subtotal —
    a wrong number that is the right shape, in the right place, from a real line of the
    document. Nothing downstream can detect that; only the word boundary prevents it.
    """
    # Longest first, so "invoice date" wins over "invoice no" on a line containing both.
    joined = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True))
    regex = re.compile(rf"\b(?:{joined})\s*[:\-]?\s*(?P<value>{pattern})", re.I)

    def extract(text: str) -> list[str]:
        return [m.group("value").strip(" .\t") for m in regex.finditer(text)]

    return extract


# A money value: digits with Western (1,234,567) or South Asian lakh (12,34,567)
# grouping, optionally with paise. It must not end on a separator, and the lookahead
# stops it reading a percentage ("18%"), a registration number ("03-02-...") or a date.
_AMOUNT_VALUE = r"\d(?:[\d,]*\d)?(?:\.\d{1,2})?(?![\d%/\-]|\.\d|\s*%)"
# Currency tokens written before an amount on Pakistani / South Asian invoices.
_CURRENCY = r"(?:PKR|Rs|INR|USD|\$|₨)\.?"
# An optional rate between a label and its amount: "GST 18%", "Sales Tax @ 17%",
# "Tax (18%)". Skipping it is what keeps the *rate* out of the tax amount field.
_RATE = r"(?:[ \t]*(?:@[ \t]*)?\(?[ \t]*\d{1,2}(?:\.\d+)?[ \t]*%[ \t]*\)?)?"


def money_labelled(*labels: str) -> Extractor:
    """Find an amount on the same line as one of its labels.

    Accepts `Total: Rs. 11,800.00`, `Total PKR 11,800`, `GST 18%: Rs 1,800.00` and
    `Subtotal: 10,000.00`, and returns only the number. Stays on the label's line
    (spaces and tabs only, never a newline) so a heading like "TAX INVOICE" cannot borrow the amount
    printed on the line below it.
    """
    joined = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True))
    regex = re.compile(
        rf"\b(?:{joined})\b{_RATE}[ \t]*[:\-=]?[ \t]*(?:{_CURRENCY}[ \t]*)?"
        rf"(?P<value>{_AMOUNT_VALUE})",
        re.I,
    )

    def extract(text: str) -> list[str]:
        return [m.group("value") for m in regex.finditer(text)]

    return extract


def by_pattern(pattern: re.Pattern) -> Extractor:
    def extract(text: str) -> list[str]:
        return [m.group(0).strip() for m in pattern.finditer(text)]

    return extract


_MONEY = re.compile(r"(?:PKR|Rs\.?|₨)\s*[\d,]+(?:\.\d{2})?|\b[\d,]{4,}(?:\.\d{2})?\b")
_DATE = re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b")
_CNIC = re.compile(r"\b\d{5}-\d{7}-\d\b")
_NTN = re.compile(r"\b\d{7}(?:-\d)?\b")
_STRN = re.compile(r"\b\d{2}-\d{2}-\d{4}-\d{3}-\d{2}\b")
_IBAN = re.compile(r"\bPK\d{2}[A-Z]{4}\d{16}\b")

# Several extractors per field. Agreement between independent methods is the evidence.
FIELD_EXTRACTORS: dict[str, dict[str, list[Extractor]]] = {
    "invoice": {
        "invoice_number": [
            labelled(
                "invoice no", "invoice number", "invoice #", "bill no", pattern=r"[A-Z0-9\-/]{3,20}"
            ),
        ],
        "invoice_date": [
            labelled("invoice date", "date of invoice", "dated", pattern=r"[\d/\-.]{8,10}"),
            by_pattern(_DATE),
        ],
        "due_date": [labelled("due date", "payment due", pattern=r"[\d/\-.]{8,10}")],
        "vendor_name": [
            labelled(
                "vendor name",
                "vendor",
                "supplier name",
                "supplier",
                "seller",
                "sold by",
                "billed by",
                "bill from",
                "issued by",
                "company name",
                "from",
            )
        ],
        "vendor_ntn": [labelled("ntn", pattern=r"\d{7}(?:-\d)?"), by_pattern(_NTN)],
        "vendor_strn": [
            labelled("strn", "sales tax reg", pattern=r"[\d\-]{15,20}"),
            by_pattern(_STRN),
        ],
        "subtotal": [money_labelled("subtotal", "sub total", "sub-total", "net amount")],
        "tax": [
            money_labelled(
                "sales tax amount", "sales tax", "gst amount", "gst", "vat", "tax amount", "tax"
            )
        ],
        "discount": [money_labelled("discount")],
        "total": [
            money_labelled(
                "grand total",
                "total amount",
                "invoice total",
                "amount due",
                "amount payable",
                "net payable",
                "total",
            )
        ],
        "iban": [labelled("iban", pattern=r"PK\d{2}[A-Z]{4}\d{16}"), by_pattern(_IBAN)],
    },
    "cnic": {
        "name": [labelled("name", pattern=r"[A-Za-z .]{3,50}")],
        "cnic_number": [
            labelled("identity number", "cnic", pattern=r"\d{5}-\d{7}-\d"),
            by_pattern(_CNIC),
        ],
        "date_of_birth": [labelled("date of birth", "dob", pattern=r"[\d/\-.]{8,10}")],
        "date_of_expiry": [labelled("date of expiry", "expiry", pattern=r"[\d/\-.]{8,10}")],
    },
    "contract": {
        "party_a": [labelled("between", "party a", "first party")],
        "party_b": [labelled("and", "party b", "second party")],
        "effective_date": [
            labelled("effective date", "commencing", "with effect from", pattern=r"[\d/\-.]{8,10}")
        ],
        "expiry_date": [
            labelled("expiry date", "terminates on", "until", pattern=r"[\d/\-.]{8,10}")
        ],
        "value": [money_labelled("consideration", "contract value", "sum of")],
        "governing_law": [labelled("governing law", "governed by")],
    },
}


@dataclass
class ExtractedField:
    name: str
    value: str | None
    confidence: float
    candidates: list[str] = field(default_factory=list)
    agreement: float = 0.0
    format_ok: bool = True
    method_count: int = 0
    note: str = ""

    @property
    def disputed(self) -> bool:
        return len({c for c in self.candidates}) > 1


def _format_ok(value: str, spec_type: str) -> bool:
    if spec_type in (FieldType.MONEY, FieldType.NUMBER):
        return parse_money(value) is not None
    if spec_type == FieldType.DATE:
        return parse_date(value) is not None
    if spec_type in FIELD_FORMATS:
        return bool(FIELD_FORMATS[spec_type][0].match(value.strip()))
    return bool(value.strip())


def extract_field(
    text: str, name: str, extractors: Sequence[Extractor], schema: DocumentSchema
) -> ExtractedField:
    candidates: list[str] = []
    contributing = 0

    for extractor in extractors:
        found = extractor(text)
        if found:
            contributing += 1
            candidates.append(found[0])

    if not candidates:
        return ExtractedField(name, None, 0.0, note="no extractor found a value")

    counts = Counter(candidates)
    value, votes = counts.most_common(1)[0]
    agreement = votes / len(candidates)

    spec = schema.spec(name)
    format_ok = _format_ok(value, spec.type if spec else FieldType.STRING)

    # Weighted, and deliberately capped below 1.0. A score of 1.0 invites a downstream
    # system to treat the value as certain, and nothing here can establish that.
    confidence = round(min(0.95, 0.5 * agreement + 0.35 * float(format_ok) + 0.10), 4)

    note = ""
    if len(counts) > 1:
        note = f"extractors disagreed: {sorted(counts)}"
        confidence = round(confidence * 0.6, 4)
    if not format_ok:
        note = (note + "; " if note else "") + "value does not match the expected format"

    return ExtractedField(
        name=name,
        value=value,
        confidence=confidence,
        candidates=candidates,
        agreement=round(agreement, 4),
        format_ok=format_ok,
        method_count=contributing,
        note=note,
    )


def extract_document(text: str, document_type: str) -> dict[str, ExtractedField]:
    schema = SCHEMAS.get(document_type)
    extractors = FIELD_EXTRACTORS.get(document_type, {})
    if schema is None:
        return {}
    return {name: extract_field(text, name, fns, schema) for name, fns in extractors.items()}
