"""Invoice field extractor.

The kind of thing that gets written in twenty minutes, passes the three documents
you tried it on, and goes to production. This is the artifact Journeyman evolves.
"""

import re


def extract(document: str) -> dict:
    """Pull the key fields out of an invoice document."""
    lines = [l.strip() for l in document.splitlines() if l.strip()]

    vendor = lines[0] if lines else None

    date = None
    m = re.search(r"\d{4}-\d{2}-\d{2}", document)
    if m:
        date = m.group(0)

    total = None
    amounts = re.findall(r"[\d,]+\.\d{2}", document)
    if amounts:
        total = float(amounts[-1].replace(",", ""))

    currency = None
    for code in ("EUR", "GBP", "USD"):
        if code in document:
            currency = code
            break

    po_number = None
    m = re.search(r"PO-\d+", document)
    if m:
        po_number = m.group(0)

    return {
        "vendor": vendor,
        "date": date,
        "total": total,
        "currency": currency,
        "po_number": po_number,
    }
