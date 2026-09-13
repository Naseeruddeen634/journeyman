"""The mutation space.

An artifact is represented as a genome: one named implementation per field. A
mutation swaps one implementation. That keeps the search space small enough to
hill-climb honestly and, more importantly, makes every mutation *attributable* -
when the score moves, we know exactly which strategy moved it, which is what
feeds the memory that makes Journeyman better next time.

Each implementation is real source. Nothing here is a placeholder: the assembled
genome is written to disk and executed as an ordinary Python module.

Online, a model proposes new implementations for slots the library cannot fix.
Offline, this library is the whole search space, and it is enough to take the
demo artifact from 63% to the high nineties without a model provider.
"""

from __future__ import annotations

PREAMBLE = '''"""Invoice field extractor. Evolved by Journeyman."""

import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _num(s):
    """Parse an amount, handling both 1,234.56 and 1.234,56."""
    s = re.sub(r"[^0-9.,\\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") \\
            else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s) - s.rfind(",") - 1 == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None
'''

# ---------------------------------------------------------------- vendor

VENDOR = {
    "first_line": '''
def _vendor(doc, lines):
    return lines[0] if lines else None
''',
    "labelled_or_first_line": '''
def _vendor(doc, lines):
    for pat in (r"(?:supplier|vendor|from)\\s*[:.]*\\s*(.+)", ):
        m = re.search(pat, doc, re.I)
        if m:
            v = m.group(1).strip(" .:")
            if v:
                return v
    return lines[0] if lines else None
''',
    "labelled_then_skip_headers": '''
def _vendor(doc, lines):
    """Labelled first; otherwise the first line that is not a document title."""
    m = re.search(r"(?:supplier|vendor|from)\\s*[:.]*\\s*(.+)", doc, re.I)
    if m:
        v = m.group(1).strip(" .:")
        if v:
            return v
    HEADERS = ("invoice", "remittance", "purchase order", "***", "===", "----")
    for l in lines:
        low = l.lower()
        if any(low.startswith(h) or h in low for h in HEADERS):
            continue
        if re.fullmatch(r"[=*\\-_ ]+", l):
            continue
        return l
    return lines[0] if lines else None
''',
}

# ------------------------------------------------------------------ date

DATE = {
    "iso_only": '''
def _date(doc):
    m = re.search(r"\\d{4}-\\d{2}-\\d{2}", doc)
    return m.group(0) if m else None
''',
    "iso_and_slashes": '''
def _date(doc):
    m = re.search(r"\\d{4}-\\d{2}-\\d{2}", doc)
    if m:
        return m.group(0)
    m = re.search(r"(\\d{1,2})/(\\d{1,2})/(\\d{4})", doc)
    if m:
        d, mo, y = m.groups()
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))
    return None
''',
    "all_formats": '''
def _date(doc):
    """Every format in the corpus, day-first where ambiguous."""
    m = re.search(r"(\\d{4})-(\\d{2})-(\\d{2})", doc)
    if m:
        y, mo, d = m.groups()
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))

    m = re.search(r"(\\d{1,2})[/.](\\d{1,2})[/.](\\d{4})", doc)
    if m:
        d, mo, y = m.groups()
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))

    # 15 Mar 2024  /  15-Mar-24
    m = re.search(r"(\\d{1,2})[ \\-]([A-Za-z]{3,9})[ \\-](\\d{2,4})", doc)
    if m:
        d, mon, y = m.groups()
        mo = MONTHS.get(mon[:3].lower())
        if mo:
            y = int(y)
            if y < 100:
                y += 2000 if y < 50 else 1900
            return "%04d-%02d-%02d" % (y, mo, int(d))

    # March 15, 2024
    m = re.search(r"([A-Za-z]{3,9})\\s+(\\d{1,2}),?\\s+(\\d{4})", doc)
    if m:
        mon, d, y = m.groups()
        mo = MONTHS.get(mon[:3].lower())
        if mo:
            return "%04d-%02d-%02d" % (int(y), mo, int(d))
    return None
''',
}

# ----------------------------------------------------------------- total

TOTAL = {
    "last_number": '''
def _total(doc, lines):
    amounts = re.findall(r"[\\d,]+\\.\\d{2}", doc)
    return float(amounts[-1].replace(",", "")) if amounts else None
''',
    "largest_number": '''
def _total(doc, lines):
    vals = [_num(a) for a in re.findall(r"[\\d.,]+", doc)]
    vals = [v for v in vals if v is not None and v > 1]
    return max(vals) if vals else None
''',
    "labelled_line": '''
def _total(doc, lines):
    """The total is on the line that says it is the total."""
    LABELS = ("total due", "grand total", "total", "balance to pay",
              "amount payable", "amount of", "balance")
    for l in lines:
        low = l.lower()
        if any(lab in low for lab in LABELS):
            m = re.findall(r"[\\d][\\d.,]*", l)
            if m:
                v = _num(m[-1])
                if v is not None:
                    return v
    vals = [_num(a) for a in re.findall(r"[\\d.,]{3,}", doc)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None
''',
    "labelled_line_or_prose": '''
def _total(doc, lines):
    """Labelled line first, then prose like 'The amount of X is due'."""
    LABELS = ("total due", "grand total", "total", "balance to pay",
              "amount payable", "balance")
    for l in lines:
        low = l.lower()
        if any(lab in low for lab in LABELS):
            m = re.findall(r"[\\d][\\d.,]*", l)
            if m:
                v = _num(m[-1])
                if v is not None:
                    return v
    m = re.search(r"amount of\\s*[^\\d]{0,4}([\\d][\\d.,]*)", doc, re.I)
    if m:
        v = _num(m.group(1))
        if v is not None:
            return v
    vals = [_num(a) for a in re.findall(r"[\\d.,]{3,}", doc)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None
''',
    "last_labelled_line": '''
def _total(doc, lines):
    """The last line carrying a total-ish label. Works until a document puts
    something after the total, which is exactly the kind of assumption that
    looks fine on a small eval set."""
    LABELS = ("total", "balance", "amount due", "amount payable")
    hit = None
    for l in lines:
        low = l.lower()
        if any(lab in low for lab in LABELS):
            m = re.findall(r"[\\d][\\d.,]*", l)
            if m:
                v = _num(m[-1])
                if v is not None:
                    hit = v
    if hit is not None:
        return hit
    vals = [_num(a) for a in re.findall(r"[\\d.,]{3,}", doc)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None
''',
    "total_not_subtotal": '''
def _total(doc, lines):
    """Grand total only. 'Subtotal' contains 'total', which is the trap."""
    STRONG = ("grand total", "total due", "total payable", "amount due",
              "amount payable", "balance to pay", "balance")
    for l in lines:
        low = l.lower()
        if any(lab in low for lab in STRONG):
            m = re.findall(r"[\\d][\\d.,]*", l)
            if m:
                v = _num(m[-1])
                if v is not None:
                    return v
    for l in lines:
        low = l.lower()
        if "total" in low and "subtotal" not in low and "sub total" not in low:
            m = re.findall(r"[\\d][\\d.,]*", l)
            if m:
                v = _num(m[-1])
                if v is not None:
                    return v
    m = re.search(r"amount of\\s*[^\\d]{0,4}([\\d][\\d.,]*)", doc, re.I)
    if m:
        v = _num(m.group(1))
        if v is not None:
            return v
    vals = [_num(a) for a in re.findall(r"[\\d.,]{3,}", doc)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None
''',
}

# -------------------------------------------------------------- currency

CURRENCY = {
    "first_code": '''
def _currency(doc):
    for code in ("EUR", "GBP", "USD"):
        if code in doc:
            return code
    return None
''',
    "code_or_symbol": '''
def _currency(doc):
    for code in ("EUR", "GBP", "USD"):
        if code in doc:
            return code
    for sym, code in (("\\u20ac", "EUR"), ("\\u00a3", "GBP"), ("$", "USD")):
        if sym in doc:
            return code
    return None
''',
}

# ------------------------------------------------------------- po_number

PO = {
    "po_dash": '''
def _po(doc):
    m = re.search(r"PO-\\d+", doc)
    return m.group(0) if m else None
''',
    "po_or_labelled_ref": '''
def _po(doc):
    m = re.search(r"PO-\\d+", doc, re.I)
    if m:
        return m.group(0).upper()
    m = re.search(r"(?:po number|our ref|ref|po ref)\\s*[:.]*\\s*([A-Z0-9\\-]+)", doc, re.I)
    return m.group(1) if m else None
''',
}

SLOTS: dict[str, dict[str, str]] = {
    "vendor": VENDOR,
    "date": DATE,
    "total": TOTAL,
    "currency": CURRENCY,
    "po_number": PO,
}

# The genome the demo artifact is equivalent to. Evolution starts here.
BASELINE_GENOME = {
    "vendor": "first_line",
    "date": "iso_only",
    "total": "last_number",
    "currency": "first_code",
    "po_number": "po_dash",
}

# Which slot a failure cluster implicates. Used to target mutations rather than
# flailing at random, and it is what makes the search converge in a few rounds.
FIELD_TO_SLOT = {
    "vendor": "vendor", "date": "date", "total": "total",
    "currency": "currency", "po_number": "po_number",
}

ASSEMBLY = '''

def extract(document: str) -> dict:
    lines = [l.strip() for l in document.splitlines() if l.strip()]
    return {
        "vendor": _vendor(document, lines),
        "date": _date(document),
        "total": _total(document, lines),
        "currency": _currency(document),
        "po_number": _po(document),
    }
'''


def assemble(genome: dict[str, str]) -> str:
    """Turn a genome into a runnable Python module."""
    parts = [PREAMBLE]
    for slot in ("vendor", "date", "total", "currency", "po_number"):
        impl = genome.get(slot) or next(iter(SLOTS[slot]))
        if impl not in SLOTS[slot]:
            raise KeyError(f"no implementation {impl!r} for slot {slot!r}")
        parts.append(SLOTS[slot][impl])
    parts.append(ASSEMBLY)
    return "\n".join(parts)


def genome_options(slot: str) -> list[str]:
    return list(SLOTS[slot].keys())
