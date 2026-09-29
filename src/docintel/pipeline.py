"""End to end: classify, extract, validate, route."""

from __future__ import annotations

from .extract import classify, extract_document
from .review import ProcessedDocument, ReviewPolicy, route
from .schema import SCHEMAS


def process(
    text: str | None,
    *,
    policy: ReviewPolicy | None = None,
    document_type: str | None = None,
    line_items: list[dict] | None = None,
) -> ProcessedDocument:
    """Process one document.

    `document_type` overrides classification, for callers who already know. `line_items`
    are supplied separately because table extraction is a different problem from field
    extraction, and pretending otherwise produces a system that is bad at both.

    `text` tolerates `None` and non-`str` input rather than raising: OCR pipelines
    routinely return `None` for a page that failed to read, and a caller passing that
    straight through should get an unclassifiable document routed for human review,
    not a crash that takes down the request.
    """
    text = "" if text is None else text if isinstance(text, str) else str(text)
    classification = classify(text)
    if document_type:
        from .extract import Classification

        classification = Classification(document_type, 1.0, reason="supplied by caller")

    if classification.needs_human:
        return route(classification, {}, [], SCHEMAS.get("invoice"), policy=policy)

    schema = SCHEMAS.get(classification.document_type)
    if schema is None:
        from .extract import Classification

        unknown = Classification(
            None,
            classification.confidence,
            reason=f"no schema for document type {classification.document_type!r}",
        )
        return route(unknown, {}, [], SCHEMAS["invoice"], policy=policy)

    fields = extract_document(text, classification.document_type)

    values = {name: f.value for name, f in fields.items()}
    if line_items is not None:
        values["line_items"] = line_items
    issues = schema.validate(values)

    return route(classification, fields, issues, schema, policy=policy)
