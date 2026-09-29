"""Document intelligence tests.

Validation and routing are exact — line items either sum to the subtotal or they do not
— so the properties that decide whether a document is safe to post are asserted rather
than measured.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from docintel.extract import classify, extract_document, labelled
from docintel.pipeline import process
from docintel.review import QueueMetrics, ReviewPolicy
from docintel.schema import (
    INVOICE,
    FieldSpec,
    FieldType,
    parse_date,
    parse_money,
    validate_field,
)

INVOICE_TEXT = """TAX INVOICE
Invoice No: INV-2026-0148
Invoice Date: 11/09/2026
Due Date: 25/09/2026
Vendor: Lahore Textiles Pvt Ltd
NTN: 1234567
STRN: 03-02-9999-123-45
Subtotal: 23,700.00
Sales Tax: 4,029.00
Total: 27,729.00
IBAN: PK36SCBL0000001123456702
"""

ITEMS = [{"amount": "12000"}, {"amount": "8500"}, {"amount": "3200"}]


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("23,700.00", Decimal("23700.00")),
            ("PKR 1,500", Decimal("1500")),
            ("Rs. 99.50", Decimal("99.50")),
            ("1500", Decimal("1500")),
        ],
    )
    def test_money_tolerates_scanned_formats(self, raw, expected):
        assert parse_money(raw) == expected

    def test_unparseable_money_is_none_not_an_exception(self):
        """An unreadable amount is an extraction problem to route, not a crash."""
        assert parse_money("N/A") is None

    def test_dates_are_day_first(self):
        """Pakistan writes DD/MM/YYYY. Month-first would read 03/04/2026 as 3 April in
        one system and 4 March in another."""
        assert parse_date("03/04/2026") == dt.date(2026, 4, 3)

    def test_iso_dates_also_parse(self):
        assert parse_date("2026-04-03") == dt.date(2026, 4, 3)

    def test_an_impossible_date_is_none(self):
        assert parse_date("32/13/2026") is None


class TestFieldValidation:
    @pytest.mark.parametrize(
        ("field_type", "good", "bad"),
        [
            (FieldType.CNIC, "35202-1234567-1", "3520212345671"),
            (FieldType.NTN, "1234567", "12345"),
            (FieldType.STRN, "03-02-9999-123-45", "03-02-9999"),
            (FieldType.IBAN, "PK36SCBL0000001123456702", "PK36SCBL123"),
            (FieldType.PHONE, "0300-1234567", "123"),
        ],
    )
    def test_pakistani_identifier_formats(self, field_type, good, bad):
        spec = FieldSpec("x", field_type)
        assert validate_field(spec, good) == []
        assert validate_field(spec, bad)

    def test_a_missing_critical_field_is_an_error(self):
        issues = validate_field(FieldSpec("total", FieldType.MONEY, critical=True), None)
        assert issues[0].severity == "error"

    def test_a_missing_non_critical_field_is_a_warning(self):
        """A misread address is a nuisance; treating it like a misread total is how a
        review queue becomes a backlog nobody works."""
        issues = validate_field(FieldSpec("notes", FieldType.STRING, critical=False), None)
        assert issues[0].severity == "warning"

    def test_an_optional_missing_field_raises_nothing(self):
        assert validate_field(FieldSpec("x", required=False), None) == []


class TestCrossFieldValidation:
    def test_line_items_must_sum_to_the_subtotal(self):
        """The single most valuable check: catches a dropped line, a misread digit and
        a fabricated total, none of which per-character confidence can see."""
        values = {"subtotal": "23,700.00", "line_items": [{"amount": "12000"}, {"amount": "8500"}]}
        issues = INVOICE.validate(values)
        assert any(i.kind == "cross_field" and i.field == "subtotal" for i in issues)

    def test_correct_line_items_pass(self):
        values = {"subtotal": "23,700.00", "line_items": ITEMS}
        assert not [i for i in INVOICE.validate(values) if i.field == "subtotal"]

    def test_totals_must_be_arithmetically_consistent(self):
        values = {"subtotal": "23700", "tax": "4029", "total": "99999"}
        assert any(i.field == "total" and i.kind == "cross_field" for i in INVOICE.validate(values))

    def test_a_due_date_before_its_invoice_date_is_an_error(self):
        """A misread year, every time."""
        values = {"invoice_date": "11/09/2026", "due_date": "25/09/2020"}
        assert any(i.field == "due_date" for i in INVOICE.validate(values))

    def test_an_implausible_tax_rate_is_flagged(self):
        """A decimal point in the wrong place — the most common numeric OCR error and
        the least likely to look wrong."""
        values = {"subtotal": "23700", "tax": "402.90", "total": "24102.90"}
        assert any(i.field == "tax" for i in INVOICE.validate(values))

    def test_a_standard_tax_rate_is_not_flagged(self):
        values = {"subtotal": "23700", "tax": "4029", "total": "27729"}
        assert not [i for i in INVOICE.validate(values) if i.field == "tax"]

    def test_missing_inputs_skip_the_rule_rather_than_failing(self):
        """With no `total` to compare against, `totals_are_consistent` must skip
        rather than raise or report a spurious cross-field mismatch."""
        issues = INVOICE.validate({"subtotal": "100"})
        assert not any(i.kind == "cross_field" and i.field == "total" for i in issues)


class TestClassification:
    def test_an_invoice_is_recognised(self):
        assert classify(INVOICE_TEXT).document_type == "invoice"

    def test_an_unknown_document_is_refused(self):
        result = classify("hello world, nothing to see")
        assert result.needs_human
        assert "no document type matched" in result.reason

    def test_an_ambiguous_document_is_refused(self):
        """An invoice attached to a contract is a real document and a real problem. A
        forced choice there looks exactly like a confident correct answer."""
        result = classify("This agreement and tax invoice subtotal hereinafter")
        assert result.needs_human
        assert "ambiguous" in result.reason

    def test_the_runner_up_is_reported(self):
        result = classify("This agreement and tax invoice subtotal hereinafter")
        assert result.runner_up


class TestExtraction:
    def test_a_label_does_not_match_inside_another_word(self):
        """The bug this caught: `total` matches inside Sub*total*, so an invoice's
        total is silently extracted as its subtotal — a wrong number of the right
        shape, in the right place, from a real line of the document."""
        fields = extract_document(INVOICE_TEXT, "invoice")
        assert fields["total"].value == "27,729.00"
        assert fields["subtotal"].value == "23,700.00"

    def test_longer_labels_win(self):
        extractor = labelled("invoice no", "invoice date", pattern=r"\S+")
        assert extractor("Invoice Date: 11/09/2026")[0] == "11/09/2026"

    def test_agreement_between_extractors_raises_confidence(self):
        """Two independent methods finding the same value is evidence; one method
        being sure is not."""
        fields = extract_document(INVOICE_TEXT, "invoice")
        assert fields["vendor_ntn"].method_count >= 2
        assert fields["vendor_ntn"].confidence > 0.7

    def test_confidence_never_reaches_one(self):
        """A score of 1.0 invites a downstream system to treat the value as certain,
        and nothing here can establish that."""
        fields = extract_document(INVOICE_TEXT, "invoice")
        assert all(f.confidence <= 0.95 for f in fields.values())

    def test_a_field_nobody_can_find_has_zero_confidence(self):
        fields = extract_document("Invoice No: X-1\n", "invoice")
        assert fields["iban"].value is None
        assert fields["iban"].confidence == 0.0

    def test_pakistani_identifiers_are_extracted(self):
        fields = extract_document(INVOICE_TEXT, "invoice")
        assert fields["vendor_strn"].value == "03-02-9999-123-45"
        assert fields["iban"].value.startswith("PK36")


class TestRouting:
    def test_a_clean_invoice_goes_straight_through(self):
        """The metric the business case rests on."""
        document = process(INVOICE_TEXT, line_items=ITEMS)
        assert document.auto_approved
        assert document.fields_needing_review == 0
        assert document.straight_through

    def test_routing_is_per_field_not_per_document(self):
        """A forty-field invoice with one uncertain field is not a failed extraction.
        Rejecting the whole document sends a human forty fields to re-key when they
        needed to check one."""
        broken = INVOICE_TEXT.replace("Total: 27,729.00", "Total: 99,999.00")
        document = process(broken, line_items=ITEMS)
        assert document.fields_needing_review == 1
        assert document.review_queue[0].field == "total"

    def test_the_queue_carries_the_specific_question(self):
        broken = INVOICE_TEXT.replace("Total: 27,729.00", "Total: 99,999.00")
        question = process(broken, line_items=ITEMS).review_queue[0].question()
        assert "27729" in question.replace(",", "") or "99999" in question.replace(",", "")

    def test_a_dropped_line_item_is_caught(self):
        document = process(INVOICE_TEXT, line_items=ITEMS[:2])
        assert any(i.field == "subtotal" for i in document.review_queue)

    def test_validation_errors_escalate_regardless_of_confidence(self):
        """An invoice whose line items do not sum to its total is wrong even if every
        character was read perfectly."""
        document = process(
            INVOICE_TEXT,
            line_items=ITEMS[:2],
            policy=ReviewPolicy(min_confidence=0.0, min_confidence_critical=0.0),
        )
        assert not document.auto_approved

    def test_an_unclassifiable_document_escalates_whole(self):
        """Nothing downstream can be trusted if the document type is wrong."""
        document = process("hello world")
        assert document.review_queue[0].field == "document_type"
        assert document.review_queue[0].critical

    def test_critical_fields_are_held_to_a_higher_bar(self):
        strict = ReviewPolicy(min_confidence=0.0, min_confidence_critical=0.99)
        document = process(INVOICE_TEXT, line_items=ITEMS, policy=strict)
        assert all(i.critical for i in document.review_queue)

    def test_a_lenient_policy_approves_more(self):
        broken = INVOICE_TEXT.replace("NTN: 1234567", "NTN: 12")
        lenient = ReviewPolicy(
            min_confidence=0.0,
            min_confidence_critical=0.0,
            escalate_on_validation_error=False,
            escalate_on_disagreement=False,
        )
        assert process(broken, line_items=ITEMS, policy=lenient).auto_approved


class TestMalformedInput:
    """An OCR/table-extraction pipeline upstream of this one routinely hands over
    `None` or wrongly-shaped data. `process()` must route that for human review,
    never crash with a raw AttributeError - a crash takes down the caller's request,
    which is exactly what this library exists to avoid."""

    def test_none_text_is_routed_for_review_not_a_crash(self):
        document = process(None)
        assert document.review_queue[0].field == "document_type"
        assert not document.auto_approved

    def test_non_string_text_is_coerced_not_a_crash(self):
        document = process(12345)
        assert document.review_queue[0].field == "document_type"

    def test_malformed_line_items_are_routed_for_review_not_a_crash(self):
        """`line_items=[100, 50]` - raw numbers instead of {"amount": ...} dicts, as a
        careless caller's table extractor might produce."""
        document = process(INVOICE_TEXT, line_items=[100, 50])
        assert any(i.field == "line_items" for i in document.review_queue)
        assert not document.auto_approved

    def test_none_line_items_entries_are_routed_for_review_not_a_crash(self):
        document = process(INVOICE_TEXT, line_items=[None, {"amount": "100"}])
        assert any(i.field == "line_items" for i in document.review_queue)


class TestQueueMetrics:
    def test_straight_through_rate_is_reported(self):
        metrics = QueueMetrics()
        metrics.add(process(INVOICE_TEXT, line_items=ITEMS))
        metrics.add(process("hello world"))
        assert metrics.summary()["straight_through_rate"] == 0.5

    def test_the_worst_field_is_identified(self):
        """Usually one field accounts for most of the queue, and fixing that one
        extractor is the whole win."""
        metrics = QueueMetrics()
        broken = INVOICE_TEXT.replace("Total: 27,729.00", "Total: 99,999.00")
        for _ in range(3):
            metrics.add(process(broken, line_items=ITEMS))
        assert next(iter(metrics.summary()["worst_fields"])) == "total"

    def test_empty_metrics_do_not_divide_by_zero(self):
        assert QueueMetrics().summary()["straight_through_rate"] == 0.0
