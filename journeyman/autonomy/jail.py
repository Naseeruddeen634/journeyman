"""An OS-level boundary around code the agent can cause to run.

The worktree and the command allowlist restrict what the agent can *ask for*.
They do not restrict what runs once the repository's tests execute, and a
shift's whole job is to run them after the agent has edited the code. A
test file the agent wrote runs with your full permissions. Probed on the
development machine with harmless canaries: test code wrote a file outside the
sandbox without any objection.

On macOS this wraps test execution in `sandbox-exec` with a profile that was
verified to enforce on macOS 15.7:

  - writes: denied everywhere except the worktree, the temp directory, /tmp
    and /dev/null. A write next to the worktree, into /Users/Shared, or into
    your home directory fails with "Operation not permitted".
  - network: denied. Nothing a test runs can send data anywhere.
  - reads: denied for credential stores (~/.ssh, ~/.aws, ~/.gnupg, keychains,
    and similar) and for Journeyman's own records of other repositories.

Network denial alone is not enough, because exfiltration can go through the
commit: test code that reads a secret and writes it into the worktree puts it
on a branch you may push. So secret-looking environment variables are removed
too, and reads of the places secrets live are denied.

`sandbox-exec` is deprecated by Apple but still enforces. Where it is not
available (Linux, or if Apple removes it) test execution is unconfined, the
shift report says so, and `journeyman doctor` reports it.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

SECRET_PATHS = [".ssh", ".aws", ".gnupg", ".docker", ".kube", ".netrc", ".npmrc", ".pypirc",
                ".config/gh", ".config/gcloud", ".azure", "Library/Keychains",
                ".journeyman/lessons.json", ".journeyman/shifts", ".journeyman/memory.json"]

SECRET_ENV = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|SESSION|COOKIE|AUTH)|"
    r"^(AWS_|OPENROUTER_|ANTHROPIC_|OPENAI_|GH_|GITHUB_|GITLAB_|HF_|HUGGING|AZURE_|GOOGLE_|GCP_)",
    re.I)


def available() -> bool:
    return sys.platform == "darwin" and SANDBOX_EXEC.exists()


def _q(p: Path | str) -> str:
    return str(p).replace("\\", "\\\\").replace('"', '\\"')


def profile(worktree: Path, extra_writable: list[Path] | None = None) -> str:
    home = Path.home().resolve()
    writable = [Path(worktree).resolve(), Path(tempfile.gettempdir()).resolve(),
                Path("/private/tmp"), *[Path(p).resolve() for p in extra_writable or []]]
    lines = ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)"]
    lines += [f'(allow file-write* (subpath "{_q(p)}"))' for p in writable]
    lines.append('(allow file-write* (literal "/dev/null") (literal "/dev/tty") '
                 '(literal "/dev/dtracehelper") (regex #"^/dev/fd/"))')
    for rel in SECRET_PATHS:
        target = home / rel
        kind = "literal" if target.suffix or rel.endswith(".netrc") or rel.endswith("rc") else "subpath"
        lines.append(f'(deny file-read* ({kind} "{_q(target)}"))')
    return "\n".join(lines)


def scrub_env(env: dict) -> dict:
    """Drop anything that looks like a credential. Keep what a test run needs."""
    keep_anyway = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL", "TERM",
                   "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "VIRTUAL_ENV"}
    return {k: v for k, v in env.items() if k in keep_anyway or not SECRET_ENV.search(k)}


def wrap(argv: list[str], worktree: Path, env: dict | None = None) -> tuple[list[str], dict, bool]:
    """(argv, env, confined). Confined is False where no OS sandbox is available."""
    clean = scrub_env(dict(env if env is not None else os.environ))
    if not available():
        return argv, clean, False
    return [str(SANDBOX_EXEC), "-p", profile(worktree), *argv], clean, True
