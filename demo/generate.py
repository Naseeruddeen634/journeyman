"""Generate the demo corpus from known records.

Journeyman's whole claim is that it can measure whether an artifact got better. That
claim is only checkable if the ground truth is exact, so the corpus here is built
backwards: start from structured records, render them into messy documents, and
keep the records as the answer key.

Nothing is graded by a language model. Every score in the demo is an exact
comparison against a value that was written before the document existed.

Run:  python demo/generate.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

VENDORS = [
    "Kelleher & Sons Ltd", "Northgate Supplies", "Aoife Byrne Catering",
    "TechParts GmbH", "Riverside Print Co", "O'Sullivan Hardware",
    "Blue Harbour Logistics", "Meridian Office Services", "Fitzgerald Timber",
    "Clearwater Sanitation", "Donnelly Electrical", "Saoirse Design Studio",
]
CITIES = ["Dublin 2", "Cork", "Galway", "Limerick", "Belfast", "Waterford"]
CURRENCIES = ["EUR", "GBP", "USD"]
SYMBOL = {"EUR": "€", "GBP": "£", "USD": "$"}


def _fmt_date(y: int, m: int, d: int, style: int) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    full = ["January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December"]
    return [
        f"{y:04d}-{m:02d}-{d:02d}",           # ISO
        f"{d:02d}/{m:02d}/{y:04d}",           # day-first, the classic trap
        f"{d} {months[m-1]} {y}",             # 15 Mar 2024
        f"{full[m-1]} {d}, {y}",              # March 15, 2024
        f"{d:02d}-{months[m-1]}-{str(y)[2:]}",  # 15-Mar-24
        f"{d:02d}.{m:02d}.{y:04d}",           # European dots
    ][style]


def _fmt_amount(cents: int, cur: str, style: int) -> str:
    whole, frac = divmod(cents, 100)
    plain = f"{whole:,}.{frac:02d}"
    euro = f"{whole:,}".replace(",", ".") + f",{frac:02d}"
    return [
        f"{SYMBOL[cur]}{plain}",              # EUR1,234.56
        f"{cur} {plain}",                     # EUR 1,234.56
        f"{plain} {cur}",                     # 1,234.56 EUR
        f"{euro} {cur}",                      # 1.234,56 EUR  <- the hard one
        f"{SYMBOL[cur]} {plain}",
    ][style]


TEMPLATES = [
    # 0: tidy
    """INVOICE

From: {vendor}
{street}
{city}

Invoice date: {date}
PO Number: {po}

{lines}

TOTAL DUE: {total}
""",
    # 1: header block, total before the lines
    """{vendor}
{city} | VAT IE{vat}

Amount payable {total}
Ref {po}   Dated {date}

Items:
{lines}
""",
    # 2: noisy, OCR-ish, label drift
    """*** REMITTANCE ADVICE ***
Supplier ..... {vendor}
Our ref ...... {po}
Date issued .. {date}

{lines}

Balance to pay:  {total}
Please quote the reference above when paying.
""",
    # 3: multi-line address, the total buried in prose
    """{vendor}
Unit 4, {street}
{city}
Ireland

Dear Customer,

Please find enclosed our invoice dated {date} under purchase order {po}.

{lines}

The amount of {total} is due within 30 days of the date above.

Regards,
Accounts Receivable
""",
    # 4: subtotal and VAT above the real total, plus a due date
    """{vendor}
{city}

Invoice date : {date}
Payment due  : {due_date}
Order ref    : {po}

{lines}

Subtotal     : {subtotal}
VAT @ 23%    : {vat_amount}
Total        : {total}
""",
    # 5: no PO at all. The right answer is nothing, not a guess.
    """{vendor}
{street}, {city}

Dated {date}

{lines}

AMOUNT DUE {total}

This is a cash sale. No purchase order was raised.
""",
    # 6: OCR noise, and the account number is the largest figure on the page
    """REMlTTANCE
Supplier: {vendor}
Account no. 4471{vat}0092
Date: {date}
Ref: {po}

{lines}

Total payable ..... {total}
""",
    # 7: table-ish, columns
    """PURCHASE ORDER CONFIRMATION
============================
VENDOR      : {vendor}
PO REF      : {po}
DATE        : {date}
LOCATION    : {city}
----------------------------
{lines}
----------------------------
GRAND TOTAL : {total}
""",
]

ITEMS = [
    "A4 paper, 5 reams", "Toner cartridge HP-26X", "Safety gloves (box of 50)",
    "Catering, 12 covers", "Pallet delivery surcharge", "Cable ties 200mm",
    "Site survey, half day", "Replacement filter unit", "Signage, 3 panels",
]


def _lines(rng: random.Random, cur: str) -> str:
    out = []
    for _ in range(rng.randint(1, 4)):
        item = rng.choice(ITEMS)
        qty = rng.randint(1, 12)
        amt = rng.randint(1500, 90000)
        out.append(f"  {qty} x {item:<32} {_fmt_amount(amt, cur, 1)}")
    return "\n".join(out)


def build_corpus(n: int = 120, seed: int = 20260914) -> list[dict]:
    """Return [{document, truth}] where truth was chosen before the text existed."""
    rng = random.Random(seed)
    cases = []
    for i in range(n):
        vendor = rng.choice(VENDORS)
        cur = rng.choice(CURRENCIES)
        y = rng.choice([1998, 2019, 2023, 2024, 2025])  # pre-2000 is a real trap
        m = rng.randint(1, 12)
        d = rng.randint(1, 28)
        cents = rng.randint(5000, 950000)
        po = f"PO-{rng.randint(10000, 99999)}"

        date_style = rng.randrange(6)
        amt_style = rng.randrange(5)
        # Template 4 (subtotal/VAT above the real total) is deliberately RARE.
        # Rare edge cases are exactly what a small hand-written eval set misses,
        # and missing them is the most common way an optimisation overfits.
        tmpl = 4 if rng.random() < 0.07 else rng.choice([0, 1, 2, 3, 5, 6, 7])

        # a second, later date that must NOT be picked
        due_y, due_m = (y, m + 1) if m < 12 else (y + 1, 1)
        subtotal_cents = int(cents / 1.23)
        vat_cents = cents - subtotal_cents

        doc = TEMPLATES[tmpl].format(
            vendor=vendor,
            due_date=_fmt_date(due_y, due_m, min(d, 28), date_style),
            subtotal=_fmt_amount(subtotal_cents, cur, amt_style),
            vat_amount=_fmt_amount(vat_cents, cur, amt_style),
            street=f"{rng.randint(1,99)} {rng.choice(['Mill','Quay','Abbey','Bridge'])} Street",
            city=rng.choice(CITIES),
            date=_fmt_date(y, m, d, date_style),
            po=po,
            total=_fmt_amount(cents, cur, amt_style),
            lines=_lines(rng, cur),
            vat=rng.randint(1000000, 9999999),
        )

        cases.append({
            "id": f"doc-{i:04d}",
            "document": doc,
            "truth": {
                "vendor": vendor,
                "date": f"{y:04d}-{m:02d}-{d:02d}",   # always normalised ISO
                "total": round(cents / 100, 2),
                "currency": cur,
                "po_number": None if tmpl == 5 else po,
            },
            "meta": {"template": tmpl, "date_style": date_style, "amount_style": amt_style},
        })
    return cases


if __name__ == "__main__":
    here = Path(__file__).parent
    cases = build_corpus()
    (here / "invoices.json").write_text(json.dumps(cases, indent=2), encoding="utf8")

    from collections import Counter
    print(f"invoices.json  {len(cases)} documents")
    print(f"  templates    {dict(Counter(c['meta']['template'] for c in cases))}")
    print(f"  date styles  {dict(Counter(c['meta']['date_style'] for c in cases))}")
    print(f"  amt styles   {dict(Counter(c['meta']['amount_style'] for c in cases))}")
    print(f"  pre-2000     {sum(1 for c in cases if c['truth']['date'] < '2000')}")
    print()
    print("--- sample ---")
    print(cases[0]["document"][:340])
    print("truth:", json.dumps(cases[0]["truth"]))
