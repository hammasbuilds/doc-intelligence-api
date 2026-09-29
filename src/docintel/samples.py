"""Sample documents used by `demo.py` and the FastAPI dashboard (the `api` extra).

Dependency-free on purpose. `demo.py` is the one command the README's base
"zero dependencies to resolve" install has to run, and it only needs a dict of
strings — pulling this out of `api.py` means importing it no longer drags in
FastAPI, Jinja2 or anything else that lives behind the `api` extra.
"""

from __future__ import annotations

SAMPLE_DOCUMENTS = {
    "clean_invoice": """TAX INVOICE
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
""",
    "broken_total_invoice": """TAX INVOICE
Invoice No: INV-2026-0201
Invoice Date: 03/09/2026
Due Date: 18/09/2026
Vendor: Karachi Electronics Traders
NTN: 7654321
Subtotal: 12,000.00
Sales Tax: 2,040.00
Total: 27,300.00
""",
    "contract": """SERVICE AGREEMENT
Party A: Faisalabad Agro Exports
Party B: NIBGE Testing Services
Effective Date: 01/06/2026
Expiry Date: 31/05/2027
Governing Law: Islamic Republic of Pakistan
""",
    "cnic": """NATIONAL IDENTITY CARD
Name: Amina Sheikh
CNIC Number: 35202-1234567-1
Date of Birth: 14/03/1994
Date of Expiry: 14/03/2032
""",
}

SAMPLE_LINE_ITEMS = {
    "clean_invoice": [{"amount": "12000"}, {"amount": "8500"}, {"amount": "3200"}],
    "broken_total_invoice": [{"amount": "6000"}, {"amount": "6000"}],
}
