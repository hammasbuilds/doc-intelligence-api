"""The FastAPI + Jinja2 demo this repo declared (the `api` extra in pyproject.toml)
but never actually built. Wraps `pipeline.process()` — no new logic, just a UI on
top of the existing library so the review-queue and straight-through-rate story
is visible without reading Python.

Run: uv run --extra api uvicorn docintel.api:app --reload --app-dir src
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .pipeline import process
from .review import QueueMetrics
from .samples import SAMPLE_DOCUMENTS, SAMPLE_LINE_ITEMS

app = FastAPI(title="doc-intelligence-api demo")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# In-memory, single-process metrics — this is a local demo, not a deployed service.
metrics = QueueMetrics()
history: list[dict] = []

# Starlette's own default for a form field/part (see starlette.formparsers); kept in
# sync here only for the friendly message below, not to change the actual limit.
MAX_FIELD_SIZE_KB = 1024


def _page(request: Request, *, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "samples": SAMPLE_DOCUMENTS,
            "result": None,
            "error": None,
            "metrics": metrics.summary(),
            "history": history,
            **context,
        },
        status_code=status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def friendly_http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
    """Starlette raises a plain HTTPException(400) when a pasted field is over the
    per-field size limit (see MAX_FIELD_SIZE_KB); without this handler the user gets
    a raw `{"detail": "..."}` body instead of the same page they were just looking at.
    """
    if exc.status_code == 400 and request.url.path == "/process":
        return _page(
            request,
            error=(
                f"That document is too large to paste into this form "
                f"(over {MAX_FIELD_SIZE_KB}KB). Use the library directly instead: "
                f"`from docintel import process`."
            ),
            status_code=400,
        )
    return HTMLResponse(str(exc.detail), status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _page(request)


@app.post("/process", response_class=HTMLResponse)
async def process_document(
    request: Request,
    text: str = Form(""),
    sample_key: str = Form(""),
) -> HTMLResponse:
    if not text.strip():
        return _page(
            request,
            submitted_text=text,
            error="Paste some document text first (or pick a sample above).",
        )

    line_items = SAMPLE_LINE_ITEMS.get(sample_key)
    document = process(text, line_items=line_items)
    metrics.add(document)

    summary = document.summary()
    history.insert(0, {"document_type": summary["document_type"], **summary})
    del history[20:]  # keep the demo view bounded

    review_queue = [
        {
            "field": item.field,
            "question": item.question(),
            "critical": item.critical,
            "confidence": item.confidence,
        }
        for item in document.review_queue
    ]

    return _page(
        request,
        submitted_text=text,
        result={
            "summary": summary,
            "values": document.values,
            "review_queue": review_queue,
            "auto_approved": document.auto_approved,
        },
    )
