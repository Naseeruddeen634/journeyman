"""Every subcommand must at least start.

A commit once left cli.py with a syntax error: two sequential edits collided
inside a try block. The installed `journeyman` command could not start at all,
and the suite that should have caught it was piped into `tail`, which swallowed
pytest's exit code. This test is the cheap part of the fix.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SUBCOMMANDS = ["build", "memory", "forget", "scout", "review", "eval", "propose", "pair", "ci",
               "mcp", "bench", "shift", "watch", "install", "uninstall", "status",
               "run-scheduled", "brain"]


def test_the_cli_module_imports():
    sys.path.insert(0, str(ROOT))
    import journeyman.cli  # noqa: F401


@pytest.mark.parametrize("cmd", SUBCOMMANDS)
def test_every_subcommand_starts(cmd):
    r = subprocess.run([sys.executable, "-m", "journeyman.cli", cmd, "--help"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    assert "usage:" in r.stdout


def test_no_subcommand_is_missing_from_this_list():
    r = subprocess.run([sys.executable, "-m", "journeyman.cli", "--help"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    listed = r.stdout.split("{", 1)[1].split("}", 1)[0].split(",")
    assert sorted(listed) == sorted(SUBCOMMANDS)
