"""Regression test for the UnicodeEncodeError that a real Windows console triggers.

Windows' default console code page (cp1252/cp437) cannot encode non-Latin script, and
this repo's own pitch is Pakistani documents - which plausibly include Urdu vendor
names. `print(doc.values["vendor_name"])` on a stock `cmd.exe`/PowerShell session used
to raise `UnicodeEncodeError: 'charmap' codec can't encode characters`.

This can't be reproduced by simply running under pytest (pytest captures stdout with
its own encoding-tolerant stream), so it spawns a real subprocess with `PYTHONIOENCODING`
forced to `cp1252` - the same failure mode a bare Windows console hits - and checks the
process the way `demo.py` now does (a defensive `sys.stdout.reconfigure(...)` before
printing) survives it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

URDU_INVOICE = """TAX INVOICE
Invoice No: INV-2026-0300
Invoice Date: 01/09/2026
Vendor: عارف کاٹن مل
Subtotal: 1,000.00
Sales Tax: 170.00
Total: 1,170.00
"""

# The exact pattern demo.py uses at startup.
RECONFIGURE_SNIPPET = (
    "try:\n"
    "    sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
    "except (AttributeError, ValueError):\n"
    "    pass\n"
)


def _run(code: str) -> subprocess.CompletedProcess:
    # PYTHONIOENCODING=cp1252 only sets the *child's* stdio encoding (the console
    # code page this test simulates); the parent must still decode whatever bytes
    # come back over the pipe as UTF-8, since that's what the child's own fix
    # (`reconfigure(encoding="utf-8", ...)`) actually writes.
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"PYTHONIOENCODING": "cp1252", "PATH": __import__("os").environ.get("PATH", "")},
    )


def test_printing_a_non_latin_value_crashes_without_the_fix():
    """Establishes the baseline failure this test guards against - a forced cp1252
    stdout (what a default Windows console gives Python) raises on Urdu text."""
    code = (
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from docintel.pipeline import process\n"
        f"doc = process({URDU_INVOICE!r})\n"
        "print(doc.values['vendor_name'])\n"
    )
    result = _run(code)
    assert result.returncode != 0
    assert "UnicodeEncodeError" in result.stderr


def test_demo_py_reconfigure_pattern_prevents_the_crash():
    code = (
        "import sys\n"
        f"{RECONFIGURE_SNIPPET}"
        "sys.path.insert(0, 'src')\n"
        "from docintel.pipeline import process\n"
        f"doc = process({URDU_INVOICE!r})\n"
        "print(doc.values['vendor_name'])\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert "عارف" in result.stdout


def test_demo_py_itself_survives_a_cp1252_console():
    """`python demo.py` - the README's own command - must not depend on the console
    already being UTF-8."""
    result = subprocess.run(
        [sys.executable, "demo.py"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"PYTHONIOENCODING": "cp1252", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert "STRAIGHT THROUGH" in result.stdout
