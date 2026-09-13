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
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _binary() -> str:
    """The installed entry point, not whatever python is on PATH right now."""
    candidate = Path(sys.executable).parent / "journeyman"
    return str(candidate) if candidate.exists() else f"{sys.executable} -m journeyman.cli"


def write_plist(repos: list[str], every_minutes: int = 120,
                max_shifts: int = 2) -> Path:
    """Write the launchd agent. Does not load it."""
    ensure()
    PLIST.parent.mkdir(parents=True, exist_ok=True)

    binary = _binary()
    args = binary.split() if " " in binary else [binary]
    args += ["run-scheduled", "--max-shifts", str(max_shifts)]
    for r in repos:
        args += ["--repo", str(Path(r).resolve())]

    plist = {
        "Label": LABEL,
        "ProgramArguments": args,
        "StartInterval": int(every_minutes * 60),
        "RunAtLoad": False,          # do not start editing the moment you install it
        "StandardOutPath": str(LOGS / "agent.out.log"),
        "StandardErrorPath": str(LOGS / "agent.err.log"),
        "WorkingDirectory": str(HOME),
        "ProcessType": "Background",  # yields to whatever you are doing
        "LowPriorityIO": True,
        "Nice": 5,
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "JOURNEYMAN_HOME": str(HOME),
        },
    }
    with PLIST.open("wb") as fh:
        plistlib.dump(plist, fh)
    return PLIST


def load() -> tuple[bool, str]:
    r = subprocess.run(["launchctl", "load", "-w", str(PLIST)],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def unload() -> tuple[bool, str]:
    r = subprocess.run(["launchctl", "unload", "-w", str(PLIST)],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def is_loaded() -> bool:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return LABEL in r.stdout


def uninstall() -> str:
    if not PLIST.exists():
        return "Nothing installed."
    unload()
    PLIST.unlink(missing_ok=True)
    return f"Removed {PLIST}. Your branches and records in {HOME} are untouched."


def status() -> dict:
    cfg = load_config()
    return {
        "installed": PLIST.exists(),
        "running": is_loaded(),
        "plist": str(PLIST),
        "home": str(HOME),
        "repos": cfg.get("repos", []),
        "log": str(LOGS / "agent.out.log"),
    }
