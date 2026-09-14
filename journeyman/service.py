"""Living in the system.

`journeyman install` writes a launchd agent so it wakes on a schedule, works
the queue in the repos you named, and leaves branches and a record behind.
That is the difference between a command you remember to run and a colleague
who is around.

What it will never do, scheduled or not: push, merge, or touch your checkout.
It creates branches. You read them. That constraint is in `guardrails.py` and
it does not relax because nobody is watching.

Removing it is one command and leaves nothing behind but the records.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from .home import HOME, LOGS, ensure, load_config

LABEL = "com.journeyman.agent"
APP = HOME / "app"

# macOS privacy protection (TCC) denies background launchd agents access to
# these folders unless the program has Full Disk Access. Proven on this
# machine: `ls ~/Downloads` from a launchd job returns "Operation not
# permitted", while ~/.journeyman and /tmp work.
PROTECTED = ("Downloads", "Documents", "Desktop", "Library/Mobile Documents")


def protected_paths(paths: list[str]) -> list[str]:
    home = Path.home().resolve()
    out = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        try:
            rel = p.relative_to(home)
        except ValueError:
            continue
        if any(str(rel) == d or str(rel).startswith(d + "/") for d in PROTECTED):
            out.append(str(p))
    return out


TCC_ADVICE = (
    "macOS blocks background agents from ~/Downloads, ~/Documents and ~/Desktop.\n"
    "    Either move the repositories somewhere like ~/code, or grant Full Disk Access to\n"
    "    {python}\n"
    "    in System Settings > Privacy & Security > Full Disk Access.\n"
    "    Install the app itself outside those folders with: journeyman install --self-contained ..."
)
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def app_python() -> Path:
    return APP / "venv" / "bin" / "python"


def build_app(source: Path, log=print) -> Path:
    """Install a copy of Journeyman under ~/.journeyman/app, where launchd can reach it.

    An editable install in ~/Downloads points launchd at code it is not allowed
    to read. A regular install copies the code into a venv that lives in the
    home directory's dot-folder, which background agents can access.
    """
    import venv

    APP.mkdir(parents=True, exist_ok=True)
    if not app_python().exists():
        log(f"  creating {APP / 'venv'}")
        venv.EnvBuilder(with_pip=True, clear=False).create(APP / "venv")
    log("  installing Journeyman and its dependencies into it (a minute or two)")
    r = subprocess.run([str(app_python()), "-m", "pip", "install", "--quiet", "--upgrade",
                        str(Path(source).resolve())], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pip install failed:\n{(r.stderr or r.stdout)[-1500:]}")
    script = app_python().parent / "journeyman"
    if not script.exists():
        raise RuntimeError(f"installed, but {script} was not created")
    import json as _json
    import time as _time
    (APP / "BUILD.json").write_text(_json.dumps({
        "source": str(Path(source).resolve()),
        "fingerprint": source_fingerprint(Path(source)),
        "built": _time.strftime("%Y-%m-%d %H:%M"),
    }, indent=2), encoding="utf8")
    return script


def source_fingerprint(source: Path) -> str:
    """A hash of the package source, so a stale copy can be noticed.

    The self-contained app is a copy. Without this, editing the source leaves
    the scheduled agent running yesterday's code with nothing to say so.
    """
    import hashlib

    h = hashlib.sha256()
    pkg = Path(source).resolve() / "journeyman"
    for f in sorted(pkg.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        h.update(str(f.relative_to(pkg)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def app_freshness() -> dict:
    """Is the installed copy the same code as its source right now?"""
    import json as _json

    build = APP / "BUILD.json"
    if not build.exists():
        return {"installed": False}
    info = _json.loads(build.read_text(encoding="utf8"))
    src = Path(info.get("source", ""))
    if not (src / "journeyman").exists():
        return {"installed": True, "stale": None, "built": info.get("built"),
                "reason": f"source {src} is not reachable from here"}
    current = source_fingerprint(src)
    return {"installed": True, "stale": current != info.get("fingerprint"),
            "built": info.get("built"), "source": str(src)}


def program_args(repos: list[str], max_shifts: int = 2, dry_run: bool = False,
                 script: Path | None = None) -> list[str]:
    """argv for launchd, as a list, never as a string to be split.

    The first version built a string and split it on spaces whenever it saw
    one, meant for the `python -m` fallback. Any install path containing a
    space, such as '~/Downloads/untitled folder/...', became argv[0] =
    '/Users/.../untitled'. launchd registered the job, `launchctl list` showed
    it, and it never ran once: runs = 0, program does not exist.
    """
    if script is not None:
        # Explicit means explicit. Falling back to the current interpreter would
        # silently point launchd back at an install it may not be allowed to read.
        args = [str(script)]
    else:
        script = Path(sys.executable).parent / "journeyman"
        if not script.exists() and (app_python().parent / "journeyman").exists():
            script = app_python().parent / "journeyman"
        args = [str(script)] if script.exists() else [sys.executable, "-m", "journeyman.cli"]
    args += ["run-scheduled", "--max-shifts", str(max_shifts)]
    for r in repos:
        args += ["--repo", str(Path(r).resolve())]
    if dry_run:
        args.append("--dry-run")
    return args


def write_plist(repos: list[str], every_minutes: int = 120,
                max_shifts: int = 2, label: str = LABEL, path: Path | None = None,
                dry_run: bool = False, run_at_load: bool = False,
                script: Path | None = None) -> Path:
    """Write a launchd agent. Does not load it."""
    ensure()
    target = path or PLIST
    target.parent.mkdir(parents=True, exist_ok=True)
    args = program_args(repos, max_shifts, dry_run, script)
    if not Path(args[0]).exists():
        raise FileNotFoundError(f"launchd would run {args[0]!r}, which does not exist")

    suffix = "" if label == LABEL else "." + label.rsplit(".", 1)[-1]
    plist = {
        "Label": label,
        "ProgramArguments": args,
        "StartInterval": int(every_minutes * 60),
        "RunAtLoad": run_at_load,    # the real agent never starts editing on install
        "StandardOutPath": str(LOGS / f"agent{suffix}.out.log"),
        "StandardErrorPath": str(LOGS / f"agent{suffix}.err.log"),
        "WorkingDirectory": str(HOME),
        "ProcessType": "Background",  # yields to whatever you are doing
        "LowPriorityIO": True,
        "Nice": 5,
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "JOURNEYMAN_HOME": str(HOME),
        },
    }
    with target.open("wb") as fh:
        plistlib.dump(plist, fh)
    return target


def load() -> tuple[bool, str]:
    r = subprocess.run(["launchctl", "load", "-w", str(PLIST)],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def unload() -> tuple[bool, str]:
    r = subprocess.run(["launchctl", "unload", "-w", str(PLIST)],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def launchd_state(label: str = LABEL) -> dict:
    """What launchd itself says: has it run, how did it exit, does the program exist.

    `launchctl list` only proves the job is registered. That check said
    'running: yes' for a job that had never executed.
    """
    import os
    import re as _re

    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"loaded": False}
    out = r.stdout
    grab = lambda key: (_re.search(rf"^\s*{key} = (.+)$", out, _re.M) or [None, None])[1]
    runs = grab("runs")
    program = grab("program")
    return {
        "loaded": True,
        "state": grab("state"),
        "runs": int(runs) if runs and runs.isdigit() else 0,
        "last_exit": grab("last exit code"),
        "program": program,
        "program_exists": bool(program) and Path(program).exists(),
    }


def verify(repos: list[str], timeout: float = 60.0, script: Path | None = None) -> tuple[bool, str]:
    """Prove launchd can actually start the agent, before trusting it overnight.

    Loads a throwaway job with the same program, arguments and environment plus
    --dry-run and RunAtLoad, waits for it to run and exit 0, then removes it.
    Nothing is edited: a dry run only checks the brain, git and the repos.
    """
    import os
    import time as _time

    label = f"{LABEL}.verify"
    path = PLIST.parent / f"{label}.plist"
    log = LOGS / "agent.verify.out.log"
    # Clear both logs. Clearing only stdout let a previous attempt's
    # "Operation not permitted" in stderr misdiagnose a job that was fine.
    log.unlink(missing_ok=True)
    (LOGS / "agent.verify.err.log").unlink(missing_ok=True)
    try:
        write_plist(repos, label=label, path=path, dry_run=True, run_at_load=True,
                    every_minutes=24 * 60, script=script)
    except FileNotFoundError as exc:
        return False, str(exc)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
    deadline = _time.time() + timeout
    state: dict = {}
    try:
        while _time.time() < deadline:
            state = launchd_state(label)
            # 'xpcproxy' and 'spawn scheduled' are launchd still starting the
            # process. An earlier version treated anything but 'running' as
            # finished and read the exit code before there was one.
            if state.get("runs", 0) >= 1 and state.get("state") == "not running" \
                    and str(state.get("last_exit", "")).lstrip("-").isdigit():
                break
            _time.sleep(0.5)
    finally:
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.unlink(missing_ok=True)

    text = log.read_text(encoding="utf8") if log.exists() else ""
    err_file = LOGS / "agent.verify.err.log"
    err = err_file.read_text(encoding="utf8") if err_file.exists() else ""
    if not state.get("runs"):
        return False, f"launchd did not start it within {timeout:.0f}s ({state})"
    if "Operation not permitted" in err + text or state.get("last_exit") == "126":
        python = program_args(repos, script=script)[0]
        return False, ("macOS denied it access (Operation not permitted).\n    "
                       + TCC_ADVICE.format(python=python))
    if state.get("last_exit") != "0" or "PREFLIGHT OK" not in text:
        return False, (f"it started but did not pass preflight (exit {state.get('last_exit')}):\n    "
                       + (err or text)[-600:].strip())
    return True, text.strip().splitlines()[-1]


def is_loaded() -> bool:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return LABEL in r.stdout


def uninstall() -> str:
    stray = PLIST.parent / f"{LABEL}.verify.plist"
    if stray.exists():
        subprocess.run(["launchctl", "unload", "-w", str(stray)], capture_output=True)
        stray.unlink(missing_ok=True)
    if not PLIST.exists():
        return "Nothing installed."
    unload()
    PLIST.unlink(missing_ok=True)
    return f"Removed {PLIST}. Your branches and records in {HOME} are untouched."


def status() -> dict:
    cfg = load_config()
    state = launchd_state()
    return {
        "installed": PLIST.exists(),
        "loaded": state.get("loaded", False),
        "runs": state.get("runs", 0),
        "last_exit": state.get("last_exit"),
        "program": state.get("program"),
        "program_exists": state.get("program_exists", False),
        "plist": str(PLIST),
        "home": str(HOME),
        "repos": cfg.get("repos", []),
        "log": str(LOGS / "agent.out.log"),
        "app": app_freshness(),
    }
