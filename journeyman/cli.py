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

from .home import HOME, MEMORY, ensure, history, load_config, record_shift, save_config

ROOT = Path(__file__).resolve().parents[1]


def _default_memory(arg: str | None) -> str:
    """Memory lives in ~/.journeyman by default, not next to the source."""
    ensure()
    return arg or str(MEMORY)


def _build(args) -> int:
    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        print(f"No corpus at {corpus_path}. Run: python demo/generate.py", file=sys.stderr)
        return 2
    corpus = json.loads(corpus_path.read_text(encoding="utf8"))
    memory = Memory(_default_memory(args.memory))
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
    memory = Memory(_default_memory(args.memory))
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
    memory = Memory(_default_memory(args.memory))
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
    failing = any(f.severity >= args.fail_on for f in findings)

    if args.format != "text":
        from . import ci
        render = {"json": ci.to_json, "github": ci.to_github, "sarif": ci.to_sarif}[args.format]
        out = render(findings)
        if out:
            print(out)
        return 1 if failing else 0

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
    return 1 if failing else 0


def _shift(args) -> int:
    """One unattended shift: take the top task, work it, report."""
    from .autonomy.guardrails import Budget
    from .autonomy.shift import report, work_one
    from .brain.models import check

    status = check()
    print(f"\n  Journeyman night shift")
    print(f"  {status.summary()}\n")
    if not (status.local_available or status.heavy_available or status.bedrock_available):
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

    # One place for every shift, manual or scheduled, so history and propose
    # see all of them. Manual runs used to write into the repo instead.
    path = record_shift(str(Path(args.repo).resolve()), result.to_dict())
    print(f"  full record: {path}\n")
    if result.outcome in ("fixed", "fixed_with_concerns"):
        print(f"  Turn it into a pull request:  journeyman propose --repo {args.repo}\n")
    return 0 if result.outcome in ("fixed", "fixed_with_concerns", "no_work") else 1


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
    if not (status.local_available or status.heavy_available or status.bedrock_available):
        print("  No brain available.\n", file=sys.stderr)
        return 2

    log = stand_watch(
        args.repo, max_shifts=args.max_shifts, max_hours=args.max_hours,
        interval_s=args.interval,
        on_shift=lambda r: print(report(r), flush=True),
    )
    print(morning_report(log))
    for s in log.shifts:
        record_shift(str(Path(args.repo).resolve()), s.to_dict())
    print(f"  {len(log.shifts)} shift record(s) written to {HOME / 'shifts'}\n")
    return 0


def _install(args) -> int:
    """Put it in the system so it runs without being asked."""
    from . import service

    repos = [str(Path(r).resolve()) for r in (args.repo or [])]
    if not repos:
        print("\n  Name at least one repo: journeyman install --repo ~/work/thing\n",
              file=sys.stderr)
        return 2
    bad = [r for r in repos if not (Path(r) / ".git").exists()]
    if bad:
        print(f"\n  Not a git repository: {bad[0]}\n", file=sys.stderr)
        return 2

    cfg = load_config()
    cfg["repos"] = repos
    save_config(cfg)

    script = None
    if args.self_contained:
        print("\n  Building a self-contained copy outside protected folders...")
        try:
            script = service.build_app(ROOT)
        except RuntimeError as exc:
            print(f"\n  Could not build the app: {exc}\n", file=sys.stderr)
            return 1
        print(f"  {script}")

    blocked = service.protected_paths(repos + [service.program_args(repos, script=script)[0]])
    if blocked:
        print("\n  Heads up: these are in folders macOS keeps from background agents:")
        for b in blocked:
            print(f"    {b}")

    print("\n  Checking that launchd can actually start it (a dry run, nothing is edited)...")
    ok, detail = service.verify(repos, script=script)
    if not ok:
        print(f"\n  NOT INSTALLED. launchd could not run the agent:\n    {detail}\n", file=sys.stderr)
        return 1
    print(f"  {detail}")

    plist = service.write_plist(repos, every_minutes=args.every, max_shifts=args.max_shifts,
                                script=script)
    print(f"\n  Wrote {plist}")
    print(f"  Wakes every {args.every} min, up to {args.max_shifts} shift(s) each time.")
    print(f"  Repos: {', '.join(repos)}")

    if args.no_start:
        print(f"\n  Not started. When you want it:  launchctl load -w {plist}\n")
        return 0
    service.unload()
    ok, msg = service.load()
    print(f"\n  {'Loaded.' if ok else 'Could not load: ' + msg} First run in {args.every} min.")
    print("  It creates branches. It never pushes, merges, or touches your checkout.")
    print("  Check on it:  journeyman status      Stop it:  journeyman uninstall\n")
    return 0 if ok else 1


def _uninstall(args) -> int:
    from . import service
    print(f"\n  {service.uninstall()}\n")
    return 0


def _status(args) -> int:
    from . import service

    st = service.status()
    print()
    print(f"  installed  {'yes' if st['installed'] else 'no'}")
    print(f"  loaded     {'yes' if st['loaded'] else 'no'}")
    if st["installed"]:
        print(f"  has run    {st['runs']} time(s), last exit {st['last_exit']}")
        if st["loaded"] and not st["program_exists"]:
            print(f"  BROKEN     launchd is set to run {st['program']!r}, which does not exist.")
            print("             Reinstall:  journeyman install --repo ...")
        elif st["loaded"] and st["runs"] == 0:
            print("  note       registered but has not run yet. That is expected only "
                  "within one interval of installing.")
    app = st.get("app") or {}
    if app.get("installed"):
        if app.get("stale"):
            print(f"  STALE      the scheduled copy was built {app['built']} and the source has "
                  "changed since. Rebuild:  journeyman install --self-contained --repo ...")
        elif app.get("stale") is False:
            print(f"  app        up to date with {app['source']} (built {app['built']})")
    print(f"  home       {st['home']}")
    for r in st["repos"]:
        print(f"  watching   {r}")
    runs = history(limit=args.limit)
    print(f"\n  {len(runs)} recent shift(s):\n" if runs else "\n  No shifts yet.\n")
    for h in runs:
        t = (h.get("task") or {}).get("title", "-")
        print(f"    {h.get('outcome','?'):<20} {t[:46]}")
        if h.get("branch"):
            print(f"    {'':<20} {h['branch']}")
    print()
    return 0


def _run_scheduled(args) -> int:
    """What launchd calls. Quiet unless something happened."""
    import time as _t

    from .autonomy.watch import morning_report, stand_watch

    cfg = load_config()
    repos = [str(Path(r).resolve()) for r in (args.repo or cfg.get("repos", []))]
    print(f"\n=== {_t.strftime('%Y-%m-%d %H:%M')} ===", flush=True)

    if args.dry_run:
        # Proves the launchd path end to end (binary, environment, PATH) without
        # touching any repository.
        import shutil as _sh

        from .brain.models import check
        st = check()
        problems = []
        if not (st.local_available or st.heavy_available or st.bedrock_available):
            problems.append(f"no brain available ({st.detail})")
        if not _sh.which("git"):
            problems.append("git is not on PATH")
        problems += [f"repo missing: {r}" for r in repos if not (Path(r) / ".git").exists()]
        print(f"  python   {sys.executable}")
        print(f"  brains   {st.summary()}")
        print(f"  repos    {', '.join(repos) or 'none'}")
        if problems:
            print("  PREFLIGHT FAILED: " + "; ".join(problems), flush=True)
            return 1
        print(f"  PREFLIGHT OK: can start, {len(repos)} repo(s), brain available", flush=True)
        return 0
    delivered = []
    for repo in repos:
        if not Path(repo).exists():
            print(f"  {repo}: gone, skipping", flush=True)
            continue
        log = stand_watch(repo, max_shifts=args.max_shifts, max_hours=1.0, interval_s=5)
        for s in log.shifts:
            record_shift(repo, s.to_dict())
            if s.outcome in ("fixed", "fixed_with_concerns"):
                delivered.append((repo, s))
        if log.shifts:
            print(f"  {repo}", flush=True)
            print(morning_report(log), flush=True)
        else:
            print(f"  {repo}: nothing to do", flush=True)

    if delivered and not args.no_notify:
        # A fix delivered at 3am is only useful if you hear about it.
        from .pair import macos_notify

        first_repo, first = delivered[0]
        flagged = sum(1 for _, s in delivered if s.concerns)
        msg = (f"{len(delivered)} fix(es) ready on branches in {Path(first_repo).name}: "
               f"{first.task.title[:60] if first.task else ''}"
               + (f" ({flagged} flagged, read the diff)" if flagged else "")
               + ". Run journeyman propose.")
        macos_notify(msg)
        print(f"  notified: {msg}", flush=True)
    return 0


def _bench(args) -> int:
    """Measure the agent against cases with hidden oracles."""
    from . import bench
    from .autonomy.guardrails import Budget

    cases = Path(args.cases) if args.cases else bench.DEFAULT_CASES
    print(f"\n  Journeyman bench   {cases}   feedback rounds: {args.feedback}   "
          f"pregather: {'on' if args.pregather else 'off'}\n", flush=True)
    summary = bench.run(
        cases, only=args.only, feedback_rounds=args.feedback,
        budget_factory=lambda: Budget(max_minutes=args.max_minutes,
                                      max_iterations=args.max_iterations),
        pregather=args.pregather,
        on_case=lambda r: print(f"  {r.case:<20} {r.outcome or 'error':<21} "
                                f"oracle {'pass' if r.oracle_passed else 'fail'}  "
                                f"{r.minutes} min", flush=True),
    )
    print(bench.render(summary))
    ensure()
    out = HOME / "bench"
    out.mkdir(exist_ok=True)
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    path = out / f"{stamp}-fb{args.feedback}{'-ctx' if args.pregather else ''}.json"
    path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf8")
    print(f"  {path}\n")
    return 0


def _pair(args) -> int:
    """Beside you while you work: affected tests on save, silent unless red."""
    from .pair import macos_notify, watch

    try:
        watch(args.repo, interval=args.interval,
              notify=macos_notify if args.notify else None)
    except KeyboardInterrupt:
        print("\n  unpaired.\n")
    return 0


def _propose(args) -> int:
    """Write the pull request description. You push and open it."""
    from .propose import propose

    r = propose(args.repo, args.branch)
    if not r["ok"]:
        print(f"\n  {r['reason']}\n", file=sys.stderr)
        return 1
    print(f"\n  {r['title']}")
    print(f"  {r['branch']} -> {r['base']}")
    if r["concerns"]:
        print(f"  The agent flagged {r['concerns']} concern(s) about its own change. "
              "They are at the top of the description.")
    print(f"\n  Description: {r['body_file']}\n")
    print("  Journeyman does not push or open pull requests.", end="")
    if r["commands"]:
        print(" When you have read the diff:\n")
        for c in r["commands"]:
            print(f"    {c}")
    else:
        print()
    for n in r.get("notes", []):
        print(f"\n  {n}")
    print()
    return 0


def _eval(args) -> int:
    """Build or run the eval harness for a prompt file."""
    from .evals import SYNTH_PROMPT, EvalDir, parse_synthesized, record_baseline, scaffold

    repo = Path(args.repo).resolve()
    prompt_path = Path(args.prompt).resolve()
    if not prompt_path.exists():
        print(f"\n  No prompt file at {prompt_path}\n", file=sys.stderr)
        return 2

    def model_call():
        from strands import Agent
        from .brain.models import pick
        model, name, why = pick("routine")
        print(f"  model: {name} ({why})")
        return (lambda text: str(Agent(model=model, callback_handler=None)(text)).strip()), name

    ev = EvalDir(repo, prompt_path)
    if not ev.cases_file.exists() or args.overwrite:
        cases = None
        if args.synthesize:
            call, _ = model_call()
            cases = parse_synthesized(call(SYNTH_PROMPT.format(n=args.synthesize, prompt=ev.prompt())))
            print(f"  synthesized {len(cases)} usable case(s)" if cases
                  else "  synthesis produced nothing usable; writing starter cases instead")
        ev = scaffold(repo, prompt_path, cases or None, overwrite=args.overwrite)
        print(f"\n  cases     {ev.cases_file}")
        print(f"  test      {ev.test_file}")
        if not cases:
            print("\n  The starter cases contain REPLACE placeholders. Edit them, then --record.")

    if args.record:
        call, name = model_call()
        result = ev.run(call=call, model=name)
        record_baseline(ev, result)
        print(f"\n  recorded {sum(result.counts.values())} response(s), baseline written")
    else:
        result = ev.run()
        if result.unrecorded:
            print(f"\n  {len(result.unrecorded)} case(s) have no recorded response. Run with --record.\n")
            return 1

    for split in ("holdout", "train"):
        if split in result.scores:
            print(f"  {split:<8} {result.scores[split]:.0%}  ({result.counts[split]} cases)")
    for f in result.failures[:5]:
        print(f"    miss [{f['split']}] {f['id']}: expected {f['expect']}, got {f['got'][:60]!r}")
    print()
    return 0


def _mcp(args) -> int:
    """Serve Journeyman's read-only tools over MCP on stdio."""
    from .mcp_server import main as serve

    serve()
    return 0


def _ci(args) -> int:
    """Write a GitHub Actions workflow that runs the review on every pull request."""
    from .ci import DEFAULT_INSTALL, init_workflow

    try:
        path, written = init_workflow(Path(args.repo).resolve(),
                                      install=args.install or DEFAULT_INSTALL,
                                      fail_on=args.fail_on, overwrite=args.overwrite)
    except ValueError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2
    if not written:
        print(f"\n  {path} already exists. Use --overwrite to replace it.\n")
        return 1
    print(f"\n  Wrote {path}")
    print("  On every pull request: findings as inline annotations on the diff, SARIF to code")
    print(f"  scanning, and the job fails on anything at severity {args.fail_on} or above.\n")
    return 0


def _brain(args) -> int:
    from .brain.models import check
    st = check()
    print()
    print(f"  local    {st.local_model:<30} {'ready' if st.local_available else 'MISSING'}")
    print(f"  heavy    {st.heavy_model:<30} {'ready' if st.heavy_available else 'not configured'}")
    print(f"  bedrock  {st.bedrock_model:<30} "
          f"{'ready (' + st.bedrock_region + ')' if st.bedrock_available else 'not configured'}")
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
    rv.add_argument("--format", choices=["text", "json", "github", "sarif"], default="text")
    rv.add_argument("--fail-on", type=int, default=80, metavar="SEVERITY",
                    help="exit 1 if any finding is at least this severe (default 80)")
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

    ins = sub.add_parser("install", help="run on a schedule, in the background")
    ins.add_argument("--repo", action="append", help="repeatable")
    ins.add_argument("--every", type=int, default=120, help="minutes between wakeups")
    ins.add_argument("--max-shifts", type=int, default=2)
    ins.add_argument("--no-start", action="store_true", help="write it but do not start it")
    ins.add_argument("--self-contained", action="store_true",
                     help="install a copy under ~/.journeyman/app, reachable by launchd")
    ins.set_defaults(func=_install)

    un = sub.add_parser("uninstall", help="remove the scheduled agent")
    un.set_defaults(func=_uninstall)

    stt = sub.add_parser("status", help="is it running, and what has it done")
    stt.add_argument("--limit", type=int, default=10)
    stt.set_defaults(func=_status)

    rs = sub.add_parser("run-scheduled", help="what the scheduler calls")
    rs.add_argument("--repo", action="append")
    rs.add_argument("--max-shifts", type=int, default=2)
    rs.add_argument("--dry-run", action="store_true", help="preflight only, edit nothing")
    rs.add_argument("--no-notify", action="store_true", help="no macOS banner when fixes are ready")
    rs.set_defaults(func=_run_scheduled)

    ev = sub.add_parser("eval", help="build or run an eval harness for a prompt file")
    ev.add_argument("prompt")
    ev.add_argument("--repo", default=".")
    ev.add_argument("--synthesize", type=int, default=0, metavar="N",
                    help="ask the model for N cases instead of starter placeholders")
    ev.add_argument("--record", action="store_true", help="call the model and record responses")
    ev.add_argument("--overwrite", action="store_true")
    ev.set_defaults(func=_eval)

    po = sub.add_parser("propose", help="write the PR description for a delivered shift")
    po.add_argument("--repo", default=".")
    po.add_argument("--branch", default=None)
    po.set_defaults(func=_propose)

    pr = sub.add_parser("pair", help="work beside you: affected tests on save, quiet unless red")
    pr.add_argument("--repo", default=".")
    pr.add_argument("--interval", type=float, default=1.0)
    pr.add_argument("--notify", action="store_true", help="macOS banner when something goes red")
    pr.set_defaults(func=_pair)

    ci = sub.add_parser("ci", help="write a GitHub Actions workflow for the review")
    ci.add_argument("action", choices=["init"])
    ci.add_argument("--repo", default=".")
    ci.add_argument("--install", default=None,
                    help="pip install target: a git URL or path, never a bare name")
    ci.add_argument("--fail-on", type=int, default=80)
    ci.add_argument("--overwrite", action="store_true")
    ci.set_defaults(func=_ci)

    mc = sub.add_parser("mcp", help="serve read-only tools to MCP clients (Claude Code, Cursor)")
    mc.set_defaults(func=_mcp)

    bn = sub.add_parser("bench", help="measure the agent against hidden oracles")
    bn.add_argument("--cases", default=None)
    bn.add_argument("--only", nargs="*", default=None)
    bn.add_argument("--feedback", type=int, default=2, help="harness feedback rounds")
    bn.add_argument("--max-minutes", type=float, default=10.0)
    bn.add_argument("--max-iterations", type=int, default=20)
    bn.add_argument("--pregather", action="store_true", help="hand the agent relevant files up front")
    bn.set_defaults(func=_bench)

    br = sub.add_parser("brain", help="which models are available")
    br.set_defaults(func=_brain)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
