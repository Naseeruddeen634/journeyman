"""Journeyman as an MCP server, so the tools you already work in can ask it things.

Claude Code, Cursor, or any MCP client can call these while you work: what an AI
engineer would flag in this repo, what is worth doing next, which tests a file
affects and whether they pass, how a prompt's eval stands, what the unattended
shifts did.

Every tool here is read-only. Editing code stays with `journeyman shift`, which
runs in an isolated worktree under guardrails and verifies its own work. Handing
a remote caller a way to start edits would be a second path around all of that.

    claude mcp add journeyman -- journeyman mcp
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

INSTRUCTIONS = """Journeyman reviews and inspects Python repositories the way an AI engineer
would. All tools are read-only. Pass `repo` as an absolute path to a directory.
Findings carry `why` and `fix`; relay those rather than paraphrasing them away.
To have Journeyman actually fix something, the user runs `journeyman shift`."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

server = MCPServer(name="journeyman", instructions=INSTRUCTIONS)


def _repo(repo: str) -> Path:
    p = Path(repo).expanduser().resolve()
    if not p.is_dir():
        # ToolError reaches the client as an error result; anything else is
        # reported as an unexpected crash in the tool.
        raise ToolError(f"not a directory: {p}")
    return p


@server.tool(annotations=READ_ONLY)
def review(repo: str, limit: int = 30) -> list[dict]:
    """AI engineering review: hardcoded model ids, unbounded output, unvalidated JSON,
    prompt injection, missing retries, live-model tests, prompts with no eval, and
    Bedrock-specific failure modes. Worst first, each with why it matters and the fix."""
    from .patterns.smells import review as run

    return [f.to_dict() for f in run(_repo(repo))[: max(1, min(limit, 200))]]


@server.tool(annotations=READ_ONLY)
def scout(repo: str, limit: int = 20) -> list[dict]:
    """What is worth doing in this repo right now, ranked: failing tests, review
    findings, backlog items, FIXMEs, untested public functions. Runs the test suite."""
    from .autonomy.scout import survey

    out = []
    for t in survey(_repo(repo))[: max(1, min(limit, 100))]:
        d = t.to_dict()
        d["evidence"] = d["evidence"][:800]
        out.append(d)
    return out


@server.tool(annotations=READ_ONLY)
def affected_tests(repo: str, file: str) -> list[str]:
    """Test files that import `file`, directly or through other modules. `file` is
    relative to the repo. Use it to know what to run after an edit."""
    from .pair import affected_tests as find

    root = _repo(repo)
    return [str(t.relative_to(root)) for t in find(root, file)]


@server.tool(annotations=READ_ONLY)
def run_affected_tests(repo: str, file: str) -> dict:
    """Run only the tests affected by `file` and report which passed and failed.
    Does not modify anything; bytecode caches are bypassed so results reflect the
    code on disk."""
    from .pair import affected_tests as find
    from .pair import run_tests

    root = _repo(repo)
    tests = find(root, file)
    passed, failed, tail = run_tests(root, tests)
    return {"tests": [str(t.relative_to(root)) for t in tests],
            "passed": sorted(passed), "failed": sorted(failed),
            "output_tail": tail if failed else ""}


@server.tool(annotations=READ_ONLY)
def eval_status(repo: str, prompt: str) -> dict:
    """Replay a prompt's recorded eval (from `journeyman eval`) and compare with its
    baseline. Never calls a model. `prompt` is the prompt file path relative to repo."""
    import json

    from .evals import EvalDir

    root = _repo(repo)
    ev = EvalDir(root, root / prompt)
    if not ev.cases_file.exists():
        return {"exists": False, "hint": f"run: journeyman eval {prompt}"}
    result = ev.run()
    baseline = (json.loads(ev.baseline_file.read_text())["scores"]
                if ev.baseline_file.exists() else {})
    return {"exists": True, "scores": result.scores, "baseline": baseline,
            "unrecorded": result.unrecorded, "failures": result.failures[:10]}


@server.tool(annotations=READ_ONLY)
def shift_history(repo: str = "", limit: int = 10) -> list[dict]:
    """Recent unattended shifts: what was attempted, the outcome, the branch, and any
    concerns the shift raised about its own change. Filter by repo if given."""
    from .home import history

    root = str(_repo(repo)) if repo else None
    out = []
    for h in history(limit=200):
        if root and str(Path(h.get("repo", "")).resolve()) != root:
            continue
        out.append({k: h.get(k) for k in ("repo", "outcome", "branch", "summary", "concerns",
                                            "finding_resolved", "feedback_rounds", "minutes")}
                   | {"task": (h.get("task") or {}).get("title")})
        if len(out) >= limit:
            break
    return out


def main() -> None:
    server.run("stdio")
