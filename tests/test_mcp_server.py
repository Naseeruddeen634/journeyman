"""The MCP server: read-only tools, callable from a real client over stdio."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from journeyman.mcp_server import server  # noqa: E402


def _call(name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    return result, texts


def test_every_tool_is_read_only():
    """Editing stays with the sandboxed shift. A remote caller gets no second path around it."""
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {
        "review", "scout", "affected_tests", "run_affected_tests", "eval_status", "shift_history"}
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


def test_review_returns_findings_with_why_and_fix():
    _, texts = _call("review", {"repo": str(ROOT / "bench/cases/aie002_max_tokens/repo")})
    findings = [json.loads(t) for t in texts]
    assert findings and findings[0]["code"] == "AIE002"
    assert len(findings[0]["why"]) > 40 and len(findings[0]["fix"]) > 20


@pytest.fixture
def project(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("")
    (tmp_path / "lib" / "money.py").write_text("def vat(n):\n    return round(n * 1.32, 2)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_money.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from lib.money import vat\n\ndef test_vat():\n    assert vat(100) == 123.0\n")
    return tmp_path


def test_affected_and_run_affected_tests(project):
    _, texts = _call("affected_tests", {"repo": str(project), "file": "lib/money.py"})
    assert texts == ["tests/test_money.py"]
    _, texts = _call("run_affected_tests", {"repo": str(project), "file": "lib/money.py"})
    out = json.loads(texts[0])
    assert out["failed"] == ["tests/test_money.py::test_vat"] and not out["passed"]
    assert "132.0" in out["output_tail"]


def test_eval_status_says_how_to_create_one(project):
    (project / "p.txt").write_text("Classify.\n")
    _, texts = _call("eval_status", {"repo": str(project), "prompt": "p.txt"})
    assert json.loads(texts[0]) == {"exists": False, "hint": "run: journeyman eval p.txt"}


def _over_stdio(calls: list[tuple[str, dict]]):
    """Spawn `journeyman mcp` and talk to it the way an IDE does."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from journeyman.autonomy.scout import fresh_env

    params = StdioServerParameters(command=sys.executable, args=["-m", "journeyman.cli", "mcp"],
                                   env=fresh_env(), cwd=str(ROOT))

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                results = [await session.call_tool(n, a) for n, a in calls]
                return names, results

    return asyncio.run(asyncio.wait_for(go(), timeout=60))


def test_a_real_client_can_use_it_over_stdio():
    """In-process calls are not how an IDE talks to it. Spawn the server and connect."""
    names, (res, bad) = _over_stdio([
        ("review", {"repo": str(ROOT / "bench/cases/aie001_model_id/repo")}),
        ("review", {"repo": "/definitely/not/here"}),
    ])
    assert "review" in names and "scout" in names
    assert any("AIE001" in getattr(c, "text", "") for c in res.content)
    # a bad path reaches the client as an error result, and the server keeps serving
    assert bad.is_error
    assert any("not a directory" in getattr(c, "text", "") for c in bad.content)
