"""Command line.

    journeyman build "extract vendor, date and total from these invoices"
    journeyman memory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graph import build_job_graph
from .memory.store import Memory
from .report import render, render_markdown
from .session import Job

ROOT = Path(__file__).resolve().parents[1]


def _build(args) -> int:
    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        print(f"No corpus at {corpus_path}. Run: python demo/generate.py", file=sys.stderr)
        return 2
    corpus = json.loads(corpus_path.read_text(encoding="utf8"))
    memory = Memory(args.memory) if args.memory else Memory()
    outdir = Path(args.out)

    job = Job(goal=args.goal, max_generations=args.max_generations)
    graph = build_job_graph(job, corpus, memory, outdir)

    print(f"\n  Journeyman   job {job.job_id}   {len(corpus)} documents\n")
    result = graph(args.goal)

    print(render(job))
    order = [n.node_id for n in result.execution_order]
    print(f"  graph: {len(order)} node executions, "
          f"{order.count('evolve')} of them the evolution loop")
    print(f"  {' -> '.join(order[:6])} -> ... -> gate -> ship\n")

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text(render_markdown(job), encoding="utf8")
    (outdir / "job.json").write_text(json.dumps(job.to_dict(), indent=2, default=str), encoding="utf8")
    print(f"  report: {outdir / 'report.md'}\n")
    return 0


def _memory(args) -> int:
    memory = Memory(args.memory) if args.memory else Memory()
    st = memory.stats()
    print(f"\n  {memory.path}")
    print(f"  {st['episodes']} job(s), {st['lessons']} lesson(s), "
          f"mean gain {st['mean_gain']:.1%}\n")
    for e in memory.episodes:
        print(f"  {e.task[:60]}")
        print(f"    {e.baseline_heldout:.1%} -> {e.shipped_heldout:.1%} "
              f"in {e.generations} generations, {len(e.lessons)} lesson(s)")
        for l in e.lessons:
            print(f"      {l.field_or_slot:<8} {l.repair:<26} +{l.gain:.3f}")
    print()
    return 0


def _forget(args) -> int:
    memory = Memory(args.memory) if args.memory else Memory()
    n = len(memory.episodes)
    memory.episodes = []
    memory.save()
    print(f"Forgot {n} episode(s). Next run starts cold.")
    return 0


def _scout(args) -> int:
    from .autonomy.scout import describe, survey
    tasks = survey(args.repo)
    print(f"\n  {describe(tasks)}\n")
    for t in tasks[: args.limit]:
        print(f"  [{t.priority:>3}] {t.kind:<14} {t.title[:60]}")
        print(f"        {t.where}")
    print()
    return 0


def _review(args) -> int:
    """Review this repo the way an AI engineer would."""
    from .patterns.smells import review, summarise

    findings = review(args.repo)
    print(f"\n  {summarise(findings)}\n")
    if not findings:
        print("  Nothing to flag.\n")
        return 0
    for f in findings[: args.limit]:
        print(f"  [{f.severity:>2}] {f.code}  {f.title}")
        print(f"        {f.where}")
        if args.verbose:
            import textwrap
            for line in textwrap.wrap(f.why, 66):
                print(f"        {line}")
            print()
            for line in textwrap.wrap("Fix: " + f.fix, 66):
                print(f"        {line}")
        print()
    if not args.verbose:
        print("  Run with --verbose for the reasoning and the fix.\n")
    return 1 if any(f.severity >= 80 for f in findings) else 0


def _shift(args) -> int:
    """One unattended shift: take the top task, work it, report."""
    from .autonomy.guardrails import Budget
    from .autonomy.shift import report, work_one
    from .brain.models import check

    status = check()
    print(f"\n  Journeyman night shift")
    print(f"  {status.summary()}\n")
    if not (status.local_available or status.heavy_available):
        print("  No brain available. Nothing will happen.\n", file=sys.stderr)
        return 2

    budget = Budget(
        max_minutes=args.max_minutes,
        max_files_changed=args.max_files,
        max_commands=args.max_commands,
        max_iterations=args.max_iterations,
    )
    result = work_one(args.repo, budget=budget, keep_worktree=not args.cleanup)
    print(report(result))

    outdir = Path(args.repo) / ".journeyman" / "shifts"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    (outdir / f"{stamp}.json").write_text(
        json.dumps(result.to_dict(), indent=2, default=str), encoding="utf8")
    print(f"  full record: {outdir / (stamp + '.json')}\n")
    return 0 if result.outcome in ("fixed", "no_work") else 1


def _watch(args) -> int:
    """Stand watch: keep working the queue until it is empty or it stops helping."""
    from .autonomy.shift import report
    from .autonomy.watch import morning_report, stand_watch
    from .brain.models import check

    status = check()
    print(f"\n  Journeyman standing watch on {args.repo}")
    print(f"  {status.summary()}")
    print(f"  up to {args.max_shifts} shifts over {args.max_hours}h, "
          f"checking every {args.interval}s\n")
    if not (status.local_available or status.heavy_available):
        print("  No brain available.\n", file=sys.stderr)
        return 2

    log = stand_watch(
        args.repo, max_shifts=args.max_shifts, max_hours=args.max_hours,
        interval_s=args.interval,
        on_shift=lambda r: print(report(r), flush=True),
    )
    print(morning_report(log))
    outdir = Path(args.repo) / ".journeyman" / "watch"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    (outdir / f"{stamp}.json").write_text(
        json.dumps(log.to_dict(), indent=2, default=str), encoding="utf8")
    print(f"  full record: {outdir / (stamp + '.json')}\n")
    return 0


def _brain(args) -> int:
    from .brain.models import check
    st = check()
    print()
    print(f"  local   {st.local_model:<28} {'ready' if st.local_available else 'MISSING'}")
    print(f"  heavy   {st.heavy_model:<28} {'ready' if st.heavy_available else 'not configured'}")
    if st.detail:
        print(f"\n  {st.detail}")
    if not st.heavy_available:
        print("  set OPENROUTER_API_KEY to enable Kimi K3 for the hard tasks")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="journeyman",
                                description="An AI engineer that builds it, proves it, and remembers.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="do an engineering job end to end")
    b.add_argument("goal")
    b.add_argument("--corpus", default=str(ROOT / "demo" / "invoices.json"))
    b.add_argument("--out", default=str(ROOT / "runs" / "latest"))
    b.add_argument("--memory", default=None)
    b.add_argument("--max-generations", type=int, default=8)
    b.set_defaults(func=_build)

    m = sub.add_parser("memory", help="what it has learned so far")
    m.add_argument("--memory", default=None)
    m.set_defaults(func=_memory)

    f = sub.add_parser("forget", help="clear memory and start cold")
    f.add_argument("--memory", default=None)
    f.set_defaults(func=_forget)

    sc = sub.add_parser("scout", help="what is worth doing in this repo right now")
    sc.add_argument("--repo", default=".")
    sc.add_argument("--limit", type=int, default=12)
    sc.set_defaults(func=_scout)

    rv = sub.add_parser("review", help="AI engineering review of this repo")
    rv.add_argument("--repo", default=".")
    rv.add_argument("--limit", type=int, default=20)
    rv.add_argument("-v", "--verbose", action="store_true")
    rv.set_defaults(func=_review)

    sh = sub.add_parser("shift", help="work one task unattended and report")
    sh.add_argument("--repo", default=".")
    sh.add_argument("--max-minutes", type=float, default=45.0)
    sh.add_argument("--max-files", type=int, default=12)
    sh.add_argument("--max-commands", type=int, default=120)
    sh.add_argument("--max-iterations", type=int, default=8)
    sh.add_argument("--cleanup", action="store_true", help="remove the worktree afterwards")
    sh.set_defaults(func=_shift)

    w = sub.add_parser("watch", help="stand watch and keep working the queue")
    w.add_argument("--repo", default=".")
    w.add_argument("--max-shifts", type=int, default=6)
    w.add_argument("--max-hours", type=float, default=8.0)
    w.add_argument("--interval", type=float, default=60.0)
    w.set_defaults(func=_watch)

    br = sub.add_parser("brain", help="which models are available")
    br.set_defaults(func=_brain)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
