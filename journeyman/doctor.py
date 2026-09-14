"""journeyman doctor: every failure found the hard way, checked on purpose.

Each check here exists because the failure it looks for was real and silent:

  - Ollama ran with a 4096-token window and discarded the task mid-shift.
  - The scheduled agent was registered and ran zero times.
  - macOS blocks background agents from ~/Downloads, ~/Documents, ~/Desktop.
  - The self-contained copy keeps running old code after the source changes.
  - A repo arrived red, so every honest fix looked STUCK until the gate changed.
  - The laptop slept for three and a half hours in the middle of a benchmark.

A check reports ok, warn or fail, says what it saw, and says what to do.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    name: str
    status: str          # ok | warn | fail | skip
    detail: str
    fix: str = ""


def _ollama_context() -> Check:
    """Is the local model actually running with the window Journeyman asks for?"""
    from .brain.models import LOCAL_CTX, LOCAL_MODEL, OLLAMA_HOST

    try:
        import ollama
        client = ollama.Client(host=OLLAMA_HOST)
        names = {m.get("model") or m.get("name") for m in client.list().get("models", [])}
    except Exception as exc:
        return Check("local model", "fail", f"Ollama unreachable at {OLLAMA_HOST}: {type(exc).__name__}",
                     "Start Ollama, or configure Bedrock or OPENROUTER_API_KEY instead.")
    if not any(n and n.startswith(LOCAL_MODEL.split(":")[0]) for n in names):
        return Check("local model", "fail", f"{LOCAL_MODEL} is not pulled",
                     f"ollama pull {LOCAL_MODEL}")
    if LOCAL_CTX < 8192:
        return Check("local model", "warn",
                     f"{LOCAL_MODEL} ready, but JOURNEYMAN_LOCAL_CTX={LOCAL_CTX}",
                     "Shifts routinely exceed 4-8K tokens; below that Ollama discards the "
                     "start of the prompt. Use 16384 or more.")
    try:
        ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        ps = ""
    loaded = [l for l in ps.splitlines() if l.startswith(LOCAL_MODEL.split(":")[0])]
    if loaded and " 4096 " in f" {loaded[0]} ":
        return Check("local model", "warn",
                     f"{LOCAL_MODEL} is currently loaded with a 4096 context by something else",
                     "It will reload at Journeyman's window on the next shift; nothing to do "
                     "unless another tool keeps it pinned at 4096.")
    return Check("local model", "ok", f"{LOCAL_MODEL} ready, requested context {LOCAL_CTX}")


def _other_brains() -> list[Check]:
    from .brain.models import check

    st = check()
    out = [Check("bedrock", "ok" if st.bedrock_available else "skip",
                 f"{st.bedrock_model} in {st.bedrock_region}" if st.bedrock_available
                 else "not configured (optional)")]
    out.append(Check("kimi k3", "ok" if st.heavy_available else "skip",
                     st.heavy_model if st.heavy_available else "OPENROUTER_API_KEY not set (optional)"))
    return out


def _tools() -> list[Check]:
    out = []
    out.append(Check("git", "ok" if shutil.which("git") else "fail",
                     shutil.which("git") or "not on PATH", "" if shutil.which("git") else "Install git."))
    try:
        import pytest  # noqa: F401
        out.append(Check("pytest", "ok", f"available to {sys.executable}"))
    except ImportError:
        out.append(Check("pytest", "fail", f"not installed for {sys.executable}",
                         "pip install pytest. Shifts run a repo's tests with it when the repo has no venv."))
    out.append(Check("gh", "ok" if shutil.which("gh") else "skip",
                     shutil.which("gh") or "not installed (optional; propose will print browser steps)"))
    return out


def _confinement() -> Check:
    """Can code a shift causes to run be kept inside the worktree?

    A shift runs the repository's tests after the agent edits the code, so a test
    file the agent wrote runs with your permissions unless an OS sandbox holds
    it. This runs a real probe rather than trusting that sandbox-exec exists:
    Apple deprecated it, and a sandbox that silently stopped enforcing would be
    worse than none.
    """
    import tempfile

    from .autonomy import jail

    if not jail.available():
        return Check("test confinement", "warn", "no OS sandbox on this platform",
                     "Shifts still work, but tests the agent edits run unconfined. Review "
                     "branches before running their code yourself, and prefer a disposable VM "
                     "or container for scheduled runs.")
    with tempfile.TemporaryDirectory() as work:
        work_p = Path(work)
        probe = Path.home() / ".journeyman" / "doctor-confinement-probe"
        probe.unlink(missing_ok=True)
        argv, env, _ = jail.wrap(["/bin/sh", "-c", f"echo x > '{probe}'; echo ok > inside"],
                                 work_p, None)
        subprocess.run(argv, cwd=work_p, capture_output=True, text=True, env=env, timeout=20)
        escaped, worked = probe.exists(), (work_p / "inside").exists()
        probe.unlink(missing_ok=True)
    if escaped:
        return Check("test confinement", "fail", "sandbox-exec ran but did NOT block a write outside",
                     "Do not run scheduled shifts until this is understood.")
    if not worked:
        return Check("test confinement", "fail", "sandbox-exec blocked writes inside the worktree too",
                     "Tests cannot run confined; check macOS version and the jail profile.")
    return Check("test confinement", "ok", "tests the agent edits run in sandbox-exec: "
                 "no writes outside the worktree, no network, no credential reads")


def _repo(repo: Path) -> list[Check]:
    from .autonomy.shift import failing_set
    from .service import protected_paths

    out = []
    if not (repo / ".git").exists():
        return [Check(f"repo {repo.name}", "fail", f"{repo} is not a git repository",
                      "Shifts need git worktrees; run journeyman inside a git repo.")]
    red, _ = failing_set(repo)
    out.append(Check(f"repo {repo.name}: tests", "ok" if not red else "warn",
                     "suite is green" if not red else f"{len(red)} test(s) already red",
                     "" if not red else "That is fine: shifts gate on new failures, and red tests "
                     "are the first thing scout will offer to work on."))
    if protected_paths([str(repo)]):
        out.append(Check(f"repo {repo.name}: schedulable", "warn",
                         "inside a folder macOS keeps from background agents",
                         "Fine for shift, pair and review. For install, move it to e.g. ~/code "
                         "or grant Full Disk Access."))
    return out


def _scheduler() -> list[Check]:
    from .service import PLIST, app_freshness, launchd_state, protected_paths

    if sys.platform != "darwin":
        return [Check("scheduler", "skip", "launchd scheduling is macOS only")]
    if not PLIST.exists():
        return [Check("scheduler", "skip", "not installed",
                      "journeyman install --self-contained --repo ~/code/<repo>")]
    st = launchd_state()
    out = []
    if not st.get("loaded"):
        out.append(Check("scheduler", "fail", "plist exists but launchd has not loaded it",
                         f"launchctl load -w {PLIST}"))
    elif not st.get("program_exists"):
        out.append(Check("scheduler", "fail", f"launchd runs {st.get('program')!r}, which does not exist",
                         "Reinstall: journeyman install --self-contained --repo ..."))
    elif st.get("last_exit") == "126":
        out.append(Check("scheduler", "fail", "last run was denied by macOS (exit 126)",
                         "Move repos out of ~/Downloads, ~/Documents, ~/Desktop or grant Full Disk Access."))
    elif st.get("runs", 0) == 0:
        out.append(Check("scheduler", "warn", "installed and loaded but has not run yet",
                         "Expected only within one interval of installing."))
    else:
        ok = st.get("last_exit") in ("0", None)
        out.append(Check("scheduler", "ok" if ok else "warn",
                         f"has run {st['runs']} time(s), last exit {st.get('last_exit')}"))
    blocked = protected_paths([st.get("program") or ""])
    if blocked:
        out.append(Check("scheduler: program", "fail", f"{blocked[0]} is in a protected folder",
                         "journeyman install --self-contained ..."))
    app = app_freshness()
    if app.get("stale"):
        out.append(Check("scheduler: app copy", "warn", f"built {app['built']}, source has changed since",
                         "journeyman install --self-contained --repo ..."))
    return out


def pmset_sleep(text: str) -> dict[str, int]:
    """{"Battery Power": minutes, "AC Power": minutes} from `pmset -g custom`. 0 means never."""
    out, section = {}, None
    for line in text.splitlines():
        if line.rstrip().endswith(":") and not line.startswith(" "):
            section = line.strip().rstrip(":")
        elif section and line.split()[:1] == ["sleep"] and len(line.split()) >= 2:
            try:
                out[section] = int(line.split()[1])
            except ValueError:
                pass
    return out


def _power() -> Check:
    """Will the machine be awake when the work is supposed to happen?

    The development laptop idle-slept after one minute on power and on battery,
    and a lid close in the middle of a benchmark paused it for 214 minutes. launchd
    does not wake a sleeping Mac for a StartInterval job, so "works while you are
    away" quietly becomes "works while you are away and the machine happens to be up".
    Journeyman never changes power settings; this only says what they are.
    """
    if sys.platform != "darwin" or not shutil.which("pmset"):
        return Check("stays awake", "skip", "power settings are only checked on macOS")
    r = subprocess.run(["pmset", "-g", "custom"], capture_output=True, text=True, timeout=10)
    sleep = pmset_sleep(r.stdout)
    ac = sleep.get("AC Power")
    if ac is None:
        return Check("stays awake", "skip", "could not read pmset settings")
    if ac == 0:
        return Check("stays awake", "ok", "does not idle-sleep on power (a closed lid still sleeps)")
    battery = sleep.get("Battery Power")
    return Check(
        "stays awake", "warn",
        f"idle-sleeps after {ac} min on power" + (f", {battery} min on battery" if battery is not None else "")
        + "; scheduled shifts do not run while it sleeps and a running one pauses",
        "For unattended shifts keep it on power with the lid open and turn on System Settings > "
        "Battery > Options > Prevent automatic sleeping on power adapter when the display is off, "
        "or install on a machine that stays up.")


def run(repo: str | Path | None = None) -> list[Check]:
    checks: list[Check] = []
    for fn in (_ollama_context, ):
        try:
            checks.append(fn())
        except Exception as exc:
            checks.append(Check(fn.__name__.strip("_"), "fail", f"check crashed: {exc}"))
    try:
        checks.append(_confinement())
    except Exception as exc:
        checks.append(Check("test confinement", "fail", f"check crashed: {exc}"))
    try:
        checks.append(_power())
    except Exception as exc:
        checks.append(Check("stays awake", "fail", f"check crashed: {exc}"))
    for group in (_other_brains, _tools, _scheduler):
        try:
            checks.extend(group())
        except Exception as exc:
            checks.append(Check(group.__name__.strip("_"), "fail", f"check crashed: {exc}"))
    if repo:
        try:
            checks.extend(_repo(Path(repo).resolve()))
        except Exception as exc:
            checks.append(Check("repo", "fail", f"check crashed: {exc}"))
    return checks


def render(checks: list[Check]) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": " -- "}
    lines = [""]
    for c in checks:
        lines.append(f"  [{mark[c.status]}] {c.name:<32} {c.detail}")
        if c.fix and c.status in ("warn", "fail"):
            lines.append(f"  {'':<39} -> {c.fix}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines += ["", f"  {fails} failing, {warns} warning(s)." if fails or warns else "  All good.", ""]
    return "\n".join(lines)
