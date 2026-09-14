"""Pair mode on TypeScript and JavaScript projects.

The graph and the parser are tested without Node. The last test runs a real
Vitest project and is skipped unless JOURNEYMAN_VITEST_PROJECT points at one
with vitest installed (it was run against Vitest 3.2.7 during development).
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from journeyman import pair
from journeyman.pair import (PairState, affected_js_tests, is_js_test, js_import_graph,
                             js_runner, run_js_tests)


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "package.json", json.dumps({"devDependencies": {"vitest": "^3.2.0"}}))
    write(tmp_path, "src/money.ts", "export const vat = (n: number) => n * 1.23;\n")
    write(tmp_path, "src/invoice.ts", 'import { vat } from "./money.js";\nexport const total = vat;\n')
    write(tmp_path, "src/util/index.ts", "export const id = (x: unknown) => x;\n")
    write(tmp_path, "src/unrelated.ts",
          '// import { vat } from "./money"\nconst s = "import x from \'./money\'";\n'
          'import { id } from "./util";\nexport default id;\n')
    write(tmp_path, "tests/invoice.test.ts", 'import { total } from "../src/invoice";\n')
    write(tmp_path, "tests/unrelated.spec.ts", 'import u from "../src/unrelated";\n')
    write(tmp_path, "node_modules/vitest/index.js", 'require("./x");\n')
    return tmp_path.resolve()


def test_runner_comes_from_package_json(tmp_path):
    assert js_runner(tmp_path) is None
    write(tmp_path, "package.json", json.dumps({"devDependencies": {"jest": "29"}}))
    assert js_runner(tmp_path) == "jest"
    write(tmp_path, "package.json", "{ not json")
    assert js_runner(tmp_path) is None


def test_test_file_naming():
    assert is_js_test(Path("a/b.test.ts")) and is_js_test(Path("b.spec.jsx"))
    assert is_js_test(Path("src/__tests__/b.ts"))
    assert not is_js_test(Path("src/testing.ts")) and not is_js_test(Path("src/contest.ts"))


def test_graph_resolves_js_specifiers_to_ts_and_index_files(project):
    g = js_import_graph(project)
    assert g[project / "src/invoice.ts"] == {project / "src/money.ts"}      # "./money.js" -> money.ts
    assert g[project / "src/unrelated.ts"] == {project / "src/util/index.ts"}
    assert not any("node_modules" in str(f) for f in g)


def test_imports_inside_comments_and_strings_are_not_edges(project):
    g = js_import_graph(project)
    assert project / "src/money.ts" not in g[project / "src/unrelated.ts"]


def test_affected_tests_follow_imports_transitively(project):
    assert affected_js_tests(project, "src/money.ts") == [project / "tests/invoice.test.ts"]
    assert affected_js_tests(project, "src/util/index.ts") == [project / "tests/unrelated.spec.ts"]
    assert affected_js_tests(project, "tests/invoice.test.ts") == [project / "tests/invoice.test.ts"]


REPORT = {
    "testResults": [
        {"name": "{root}/tests/invoice.test.ts", "status": "failed", "assertionResults": [
            {"fullName": "invoice adds vat", "status": "failed",
             "failureMessages": ["AssertionError: expected 132 to be 123\n    at tests/invoice.test.ts:5"]},
            {"fullName": "invoice zero", "status": "passed", "failureMessages": []},
            {"fullName": "invoice later", "status": "skipped", "failureMessages": []}]},
        {"name": "{root}/tests/broken.test.ts", "status": "failed", "assertionResults": [],
         "message": "SyntaxError: Unexpected token\nmore"},
    ]
}


def fake_runner(report):
    def run(argv, **kw):
        out = next(a.split("=", 1)[1] for a in argv if a.startswith("--outputFile="))
        if report is not None:
            Path(out).write_text(json.dumps(report).replace("{root}", str(kw["cwd"])))

        class R:
            returncode, stdout, stderr = 1, 'console.log from a test: {"not": "the report"}', \
                "Error: Cannot find module 'vitest'"
        return R()
    return run


def test_report_is_parsed_into_node_ids(project, monkeypatch):
    monkeypatch.setattr(pair.subprocess, "run", fake_runner(REPORT))
    passed, failed, first = run_js_tests(project, [project / "tests/invoice.test.ts"], "vitest")
    assert passed == {"tests/invoice.test.ts::invoice zero", pair.DID_NOT_RUN}
    assert failed == {"tests/invoice.test.ts::invoice adds vat",
                      "tests/broken.test.ts::(suite failed to load)"}
    assert first == "AssertionError: expected 132 to be 123"


def test_a_runner_that_produces_no_report_is_red_not_silent(project, monkeypatch):
    monkeypatch.setattr(pair.subprocess, "run", fake_runner(None))
    passed, failed, first = run_js_tests(project, [project / "tests/invoice.test.ts"], "vitest")
    assert not passed and failed == {pair.DID_NOT_RUN}
    assert "Cannot find module" in first


def test_python_saves_do_not_invoke_node(project, monkeypatch):
    calls = []
    monkeypatch.setattr(pair, "run_js_tests", lambda *a, **k: calls.append(a) or (set(), set(), ""))
    write(project, "tool.py", "x = 1\n")
    PairState(project).on_save([project / "tool.py"])
    assert calls == []


VITEST = os.environ.get("JOURNEYMAN_VITEST_PROJECT")


@pytest.mark.skipif(not (VITEST and (Path(VITEST) / "node_modules/vitest").exists()
                         and shutil.which("npx")),
                    reason="set JOURNEYMAN_VITEST_PROJECT to a directory with vitest installed")
def test_real_vitest_red_and_green(tmp_path):
    src = Path(VITEST)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "node_modules").symlink_to(src / "node_modules")
    shutil.copy(src / "package.json", root / "package.json")
    write(root, "src/money.ts", "export function vat(n: number): number {\n  return n * 1.23;\n}\n")
    write(root, "src/invoice.ts", 'import { vat } from "./money";\nexport const total = (n: number) => vat(n);\n')
    write(root, "tests/invoice.test.ts",
          'import { describe, it, expect } from "vitest";\nimport { total } from "../src/invoice";\n'
          'describe("invoice", () => { it("adds vat", () => { expect(total(100)).toBe(123); }); });\n')
    state = PairState(root)
    state.prime()
    assert state.status == {"tests/invoice.test.ts::invoice adds vat": "pass", pair.DID_NOT_RUN: "pass"}

    write(root, "src/money.ts", "export function vat(n: number): number {\n  return n * 1.5;\n}\n")
    msgs = state.on_save([root / "src/money.ts"])
    assert msgs and msgs[0].startswith("RED after saving money.ts: tests/invoice.test.ts::invoice adds vat")
    assert "expected 150 to be 123" in msgs[0]

    write(root, "src/money.ts", "export function vat(n: number): number {\n  return n * 1.23;\n}\n")
    assert state.on_save([root / "src/money.ts"]) == ["green again: tests/invoice.test.ts::invoice adds vat"]
