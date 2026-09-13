"""Where Journeyman lives once it is installed.

Everything that has to survive being installed, moved, or run from somewhere
else belongs here. The old code resolved paths relative to the source tree,
which works exactly until you install it, at which point the demo corpus is
inside site-packages and the memory file is wherever you happened to be
standing.

    ~/.journeyman/
        memory.json          what it has learned, across every repo
        config.json          which models, which repos, how often
        shifts/              every unattended run, newest last
        logs/                stdout from the scheduled service
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

HOME = Path(os.environ.get("JOURNEYMAN_HOME", Path.home() / ".journeyman"))

MEMORY = HOME / "memory.json"
CONFIG = HOME / "config.json"
SHIFTS = HOME / "shifts"
LOGS = HOME / "logs"

DEFAULT_CONFIG = {
    "repos": [],
    "local_model": "qwen3-coder:30b",
    "heavy_model": "moonshotai/kimi-k3",
    "bedrock_model": "global.anthropic.claude-sonnet-4-6",
    "bedrock_region": "us-west-2",
    "max_shifts_per_run": 3,
    "max_minutes_per_shift": 20,
}


def ensure() -> Path:
    for d in (HOME, SHIFTS, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf8")
    return HOME


def load_config() -> dict:
    ensure()
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf8"))
    except (json.JSONDecodeError, OSError):
        cfg = {}
    return {**DEFAULT_CONFIG, **cfg}


def save_config(cfg: dict) -> None:
    ensure()
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf8")


def record_shift(repo: str, payload: dict) -> Path:
    """One file per shift, in one place, so history is greppable."""
    ensure()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "".join(c if c.isalnum() else "-" for c in Path(repo).name)[:40]
    path = SHIFTS / f"{stamp}_{name}.json"
    path.write_text(json.dumps({"repo": str(repo), **payload}, indent=2, default=str),
                    encoding="utf8")
    return path


def history(limit: int = 20) -> list[dict]:
    ensure()
    out = []
    for p in sorted(SHIFTS.glob("*.json"), reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text(encoding="utf8"))
            d["_file"] = str(p)
            out.append(d)
        except (json.JSONDecodeError, OSError):
            continue
    return out
