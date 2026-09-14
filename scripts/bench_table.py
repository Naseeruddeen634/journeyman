"""Render saved benchmark runs as a markdown table, straight from the JSON.

Numbers in a README drift from the runs that produced them when they are typed
by hand. This reads ~/.journeyman/bench/*.json and prints the table.

    python scripts/bench_table.py RUN.json [RUN.json ...] --labels "a" "b"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf8"))


def summary_rows(runs: list[tuple[str, dict]]) -> list[str]:
    out = ["| run | correct | delivered | gamed | withheld | minutes |",
           "|---|---|---|---|---|---|"]
    for label, r in runs:
        scored = r["cases"] - r["errors"]
        out.append(f"| {label} | **{r['correct']}/{scored}** | {r['delivered']} | {r['gamed']} | "
                   f"{r['withheld']} | {r['minutes']} |")
    return out


def per_case_rows(runs: list[tuple[str, dict]]) -> list[str]:
    cases = [c["case"] for c in runs[0][1]["results"]]
    out = ["| case | " + " | ".join(label for label, _ in runs) + " |",
           "|---|" + "---|" * len(runs)]
    for name in cases:
        cells = []
        for _, r in runs:
            c = next((x for x in r["results"] if x["case"] == name), None)
            if c is None or c.get("error"):
                cells.append("error")
            elif c["correct"]:
                cells.append("correct")
            elif c["gamed"]:
                cells.append("**gamed**")
            else:
                cells.append("withheld")
        out.append(f"| `{name}` | " + " | ".join(cells) + " |")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--labels", nargs="+")
    a = ap.parse_args()
    labels = a.labels or [Path(p).stem for p in a.runs]
    runs = list(zip(labels, [load(p) for p in a.runs]))
    print("\n".join(summary_rows(runs)))
    print()
    print("\n".join(per_case_rows(runs)))


if __name__ == "__main__":
    main()
