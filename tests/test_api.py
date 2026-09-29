"""Smoke tests for the FastAPI + Jinja2 demo (the `api` extra)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
# starlette's TestClient refuses to import without this; skip rather than error so a
# local run with only the `api` extra installed still collects the rest of the suite.
pytest.importorskip("httpx2")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from docintel.api import SAMPLE_DOCUMENTS, app  # noqa: E402

client = TestClient(app)


def test_index_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "doc-intelligence-api" in response.text


def test_clean_invoice_auto_approves():
    response = client.post(
        "/process",
        data={"text": SAMPLE_DOCUMENTS["clean_invoice"], "sample_key": "clean_invoice"},
    )
    assert response.status_code == 200
    assert "straight-through" in response.text


def test_broken_total_invoice_is_flagged():
    response = client.post(
        "/process",
        data={
            "text": SAMPLE_DOCUMENTS["broken_total_invoice"],
            "sample_key": "broken_total_invoice",
        },
    )
    assert response.status_code == 200
    assert "need review" in response.text or "total" in response.text.lower()


def test_contract_sample_processes():
    response = client.post(
        "/process", data={"text": SAMPLE_DOCUMENTS["contract"], "sample_key": "contract"}
    )
    assert response.status_code == 200


def test_blank_submit_shows_a_friendly_message_not_a_raw_422():
    """A browser posts `text=` (empty) for an untouched <textarea> - a very ordinary
    way to misuse the form. This must render the page with an inline message, not
    FastAPI's default `{"detail": [...]}` validation-error JSON."""
    response = client.post("/process", data={"text": "", "sample_key": ""})
    assert response.status_code == 200
    assert "detail" not in response.text
    assert "paste" in response.text.lower()


def test_whitespace_only_submit_is_also_treated_as_blank():
    response = client.post("/process", data={"text": "   \n\t  ", "sample_key": ""})
    assert response.status_code == 200
    assert "paste" in response.text.lower()


def test_oversized_field_shows_a_friendly_message_not_a_raw_400():
    """Starlette caps a form field at 1MB by default and raises HTTPException(400)
    with a plain `{"detail": "..."}` body; the demo should show the same page with an
    inline message instead."""
    huge_text = "x" * (1024 * 1024 + 1)
    response = client.post("/process", data={"text": huge_text, "sample_key": ""})
    assert response.status_code == 400
    assert response.text.strip().startswith("{") is False
    assert "too large" in response.text.lower()
