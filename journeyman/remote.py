"""Journeyman as a service: review a repository on request, from anywhere.

The laptop agent reviews the repo it lives in. A team wants the same judgement
from CI, from a chat bot, or from a teammate who has no local model, without
installing anything. This is that entry point, deployed on Amazon Bedrock
AgentCore Runtime (see agentcore_app.py), where each invocation runs in its own
isolated session.

What runs where, deliberately:
  - the findings and the model inventory are deterministic: the same code that
    runs in `journeyman review` and `journeyman inventory`, no model involved,
    so a judgement never depends on a model's mood
  - a Strands agent on Bedrock only explains them: it reads the flagged code
    through a read-only, repo-confined tool and writes the concrete fix
  - nothing is executed from the repository. It is downloaded as an archive,
    read, and discarded. Running untrusted tests belongs in `journeyman shift`,
    which has its own sandbox

Only public GitHub repositories are fetched, by owner/name, so the service
cannot be pointed at internal addresses.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
                    r"(?:/tree/([A-Za-z0-9_./-]+))?/?$")

SYSTEM = """You are Journeyman, reviewing a repository for the problems a senior AI
engineer would stop at in code review.

The findings you are given were produced by deterministic checks. Do not add
findings of your own and do not dispute them without reading the code. For the
most important ones (at most three), read the flagged lines with read_file, then
explain in plain language what will go wrong in production and show the smallest
concrete change that fixes it, as a short code snippet with file:line.

If there are no findings, say so in one sentence. Be specific and brief. Never
claim you ran the code or its tests: you did not."""


class RequestError(ValueError):
    """The request cannot be served as asked; the message says why."""


@dataclass
class Repo:
    owner: str
    name: str
    ref: str

    @property
    def archive_url(self) -> str:
        return f"https://codeload.github.com/{self.owner}/{self.name}/tar.gz/{self.ref}"


def parse_repo(url: str) -> Repo:
    m = GITHUB.match((url or "").strip())
    if not m:
        raise RequestError("repo must be a public GitHub URL like https://github.com/owner/name "
                           "(optionally /tree/<branch>)")
    return Repo(m.group(1), m.group(2), m.group(3) or "HEAD")


def extract(archive: bytes, dest: Path) -> Path:
    """Unpack a GitHub tarball safely and return the repository root inside it."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise RequestError(f"repository archive is over {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")          # no absolute paths, no escapes, no devices
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RequestError("unexpected archive layout")
    return roots[0]


def fetch(repo: Repo, dest: Path, opener=urllib.request.urlopen) -> Path:
    req = urllib.request.Request(repo.archive_url, headers={"User-Agent": "journeyman-remote"})
    try:
        with opener(req, timeout=60) as resp:
            data = resp.read(MAX_ARCHIVE_BYTES + 1)
    except Exception as exc:                    # 404 for a private or missing repo, network errors
        raise RequestError(f"could not download {repo.owner}/{repo.name}@{repo.ref}: {exc}") from exc
    return extract(data, dest)


def analyse(root: Path) -> dict:
    """The deterministic part: findings and model inventory."""
    from .inventory import build
    from .patterns.smells import review

    findings = review(root)
    return {"findings": [f.to_dict() for f in findings], "inventory": build(root).to_dict()}


def read_tool(root: Path):
    from strands import tool

    root = root.resolve()

    @tool
    def read_file(path: str, start: int = 1, end: int = 120) -> str:
        """Read lines from a file in the repository under review.

        Args:
            path: Path relative to the repository root.
            start: First line to return (1-based).
            end: Last line to return.
        """
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            return f"not a file in this repository: {path}"
        lines = target.read_text(encoding="utf8", errors="ignore").splitlines()
        start, end = max(1, start), min(len(lines), end, start + 300)
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))

    return read_file


def explain(root: Path, result: dict, model=None, question: str = "") -> str:
    from strands import Agent

    if model is None:
        from strands.models import BedrockModel

        model = BedrockModel(
            model_id=os.environ.get("JOURNEYMAN_BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            max_tokens=2000, temperature=0.2)
    top = result["findings"][:12]
    brief = ("Findings (worst first):\n" + json.dumps(
        [{k: f[k] for k in ("code", "title", "where", "severity", "fix")} for f in top], indent=1)
        if top else "There are no findings.")
    inv = result["inventory"]
    brief += (f"\n\nModel call sites: {len([s for s in inv['sites'] if not s['test']])}; "
              f"destinations: {sorted({s['destination'] for s in inv['sites'] if not s['test']})}")
    if question:
        brief += f"\n\nThe person asking also wants to know: {question[:500]}"
    agent = Agent(model=model, system_prompt=SYSTEM, tools=[read_tool(root)], callback_handler=None)
    return str(agent(brief)).strip()


def handle(payload: dict, model=None, fetcher=fetch) -> dict:
    """One request: {"repo": url, "mode": "review" | "explain", "prompt": optional}."""
    repo = parse_repo(payload.get("repo", ""))
    mode = payload.get("mode", "explain")
    if mode not in ("review", "explain"):
        raise RequestError('mode must be "review" (deterministic only) or "explain"')
    with tempfile.TemporaryDirectory(prefix="journeyman-remote-") as tmp:
        root = fetcher(repo, Path(tmp))
        result = analyse(root)
        out = {"repo": f"{repo.owner}/{repo.name}", "ref": repo.ref, **result}
        if mode == "explain":
            out["explanation"] = explain(root, result, model=model, question=payload.get("prompt", ""))
    return out
