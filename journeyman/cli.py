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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
