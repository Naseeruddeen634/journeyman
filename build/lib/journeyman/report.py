"""Rendering a completed job.

Written for the person who has to decide whether to trust the thing. That means
the held-out number is the headline, the training score is shown next to it so
the gap is visible, and the work log says what was tried and rejected, not just
what won.
"""

from __future__ import annotations

from .patterns.library import PATTERNS

W = 76


def _bar(v: float, width: int = 28) -> str:
    n = int(round(max(0.0, min(1.0, v)) * width))
    return "#" * n + "." * (width - n)


def render(job) -> str:
    out: list[str] = []
    out.append("=" * W)
    out.append(f"  {job.goal}")
    out.append("=" * W)
    out.append("")

    out.append("  HOW I BUILT IT")
    out.append("")
    for k in job.patterns:
        p = PATTERNS[k]
        out.append(f"    chose    {p.name}")
        out.append(f"             {p.summary}")
    for r in job.rejected[:3]:
        out.append(f"    not      {r['pattern']}: {r['why_not']}")
    out.append("")

    if job.recalled:
        out.append("  WHAT I REMEMBERED")
        out.append("")
        for r in job.recalled:
            out.append(f"    similar job ({r['similarity']:.0%} match): {r['task'][:52]}")
            out.append(f"      it gained {r['gain']:.0%} in {r['generations']} generations")
        if job.priority_repairs:
            out.append("    trying these first: "
                       + ", ".join(f"{s}={i}" for s, i, _ in job.priority_repairs))
        out.append("")
    else:
        out.append("  WHAT I REMEMBERED")
        out.append("")
        out.append("    Nothing. First time seeing this kind of job.")
        out.append("")

    b = job.baseline
    out.append("  WHERE IT STARTED")
    out.append("")
    if b:
        out.append(f"    {_bar(b.score)}  {b.score:.1%} on the eval set")
        out.append(f"    {b.train.exact_match:.1%} of documents fully correct.")
        out.append("")
        out.append("    What was actually wrong:")
        for c in b.train.failure_clusters()[:4]:
            out.append(f"      {c['count']:>3} cases  {c['field']}: {c['pattern']}")
    out.append("")

    out.append("  WHAT I TRIED")
    out.append("")
    for entry in job.ledger:
        mark = "  <- recalled" if entry.get("from_memory") else ""
        out.append(f"    gen {entry['generation']}  {entry['tried']:<34} "
                   f"{entry['train_score']:.3f}  {entry['delta']:+.3f}{mark}")
    out.append(f"    {len(job.ledger)} candidates measured.")
    out.append("")

    out.append("  WHAT SURVIVED")
    out.append("")
    if job.baseline_heldout and job.shipped_heldout:
        out.append(f"    {'before':>10}  {_bar(job.baseline_heldout.score)}  "
                   f"{job.baseline_heldout.score:.1%}")
        out.append(f"    {'after':>10}  {_bar(job.shipped_heldout.score)}  "
                   f"{job.shipped_heldout.score:.1%}")
        out.append("")
        out.append(f"    Both measured on the half of the eval set the search never saw.")
        if job.best_train and job.best_train_heldout:
            out.append(f"    Best training score was {job.best_train.score:.1%}, which held "
                       f"up at {job.best_train_heldout.score:.1%}.")
    out.append("")
    out.append(f"    {job.verdict}")
    out.append("")

    if job.lessons:
        out.append("  WHAT I LEARNED, FOR NEXT TIME")
        out.append("")
        for slot, pattern, repair, gain in job.lessons:
            out.append(f"    {slot:<8} {repair:<26} +{gain:.3f}")
            out.append(f"             fixed: {pattern[:56]}")
        out.append("")

    if job.artifacts:
        out.append("  WHAT YOU GOT")
        out.append("")
        for name, path in job.artifacts.items():
            out.append(f"    {name:<10} {path}")
        out.append("")
        out.append("    The gate is a pytest file. Run it in CI and the improvement stays.")
        out.append("")
    return "\n".join(out)


def render_markdown(job) -> str:
    md = [f"# {job.goal}\n"]
    md.append(f"**{job.verdict}**\n")
    if job.baseline_heldout and job.shipped_heldout:
        md.append("| | held-out score |")
        md.append("|---|---|")
        md.append(f"| before | {job.baseline_heldout.score:.1%} |")
        md.append(f"| after | **{job.shipped_heldout.score:.1%}** |")
        md.append("")
    md.append("## How it was built\n")
    for k in job.patterns:
        md.append(f"- **{PATTERNS[k].name}**: {PATTERNS[k].summary}")
    md.append("\nRejected:\n")
    for r in job.rejected[:4]:
        md.append(f"- {r['pattern']}: {r['why_not']}")
    md.append("")
    if job.recalled:
        md.append("## Recalled from previous runs\n")
        for r in job.recalled:
            md.append(f"- {r['similarity']:.0%} match: {r['task']} (gained {r['gain']:.0%})")
        md.append("")
    md.append("## Candidates measured\n")
    md.append("| gen | change | train | delta | from memory |")
    md.append("|---|---|---|---|---|")
    for e in job.ledger:
        md.append(f"| {e['generation']} | `{e['tried']}` | {e['train_score']:.3f} | "
                  f"{e['delta']:+.3f} | {'yes' if e.get('from_memory') else ''} |")
    md.append("")
    if job.lessons:
        md.append("## Lessons recorded\n")
        for slot, pattern, repair, gain in job.lessons:
            md.append(f"- `{slot}` -> `{repair}` (+{gain:.3f}) fixed: {pattern}")
        md.append("")
    return "\n".join(md)
