"""`review --new-since`: a PR is judged on what it introduced, not on the codebase's past."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from journeyman.ci import RefError, init_workflow, new_findings

ROOT = Path(__file__).resolve().parents[1]

OLD = textwrap.dedent('''
    from openai import OpenAI
    client = OpenAI()

    def classify(text):
        return client.chat.completions.create(model="gpt-4o-mini", max_tokens=5,
                                              messages=[{"role": "user", "content": text}])
''')


def git(repo, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "svc"
    (root / "app").mkdir(parents=True)
    (root / "app" / "classify.py").write_text(OLD)
    (root / "prompts").mkdir()
    (root / "prompts" / "triage_prompt.txt").write_text("You are a triage assistant.")
    (root / "tests").mkdir()
    (root / "tests" / "test_triage.py").write_text('PROMPT = "prompts/triage_prompt.txt"\n')
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feature")
    return root


def codes(findings):
    return sorted((f.code, f.where.split(":")[0]) for f in findings)


def test_old_findings_that_only_moved_are_not_new(repo):
    # lines inserted above the pre-existing AIE001 move it down; that is not new
    (repo / "app" / "classify.py").write_text('"""Classifier."""\nimport logging\n\n' + OLD)
    new, already = new_findings(repo, "main")
    assert new == [] and already >= 1


def test_a_finding_the_change_adds_is_reported(repo):
    (repo / "app" / "summary.py").write_text(textwrap.dedent('''
        from openai import OpenAI
        def summarise(c, text):
            return c.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": text}])
    '''))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add summary")
    new, already = new_findings(repo, "main")
    assert ("AIE001", "app/summary.py") in codes(new) and ("AIE002", "app/summary.py") in codes(new)
    assert not any(f.where.startswith("app/classify.py") for f in new)


def test_a_finding_on_an_untouched_file_caused_by_the_change_is_reported(repo):
    # deleting the test is what makes the unchanged prompt file lose its eval
    (repo / "tests" / "test_triage.py").unlink()
    new, _ = new_findings(repo, "main")
    assert codes(new) == [("AIE008", "prompts/triage_prompt.txt")]


def test_a_renamed_file_keeps_its_old_findings_old(repo):
    git(repo, "mv", "app/classify.py", "app/classifier.py")
    git(repo, "mv", "prompts/triage_prompt.txt", "prompts/triage_prompt_v1.txt")
    (repo / "tests" / "test_triage.py").write_text('PROMPT = "prompts/triage_prompt_v1.txt"\n')
    git(repo, "commit", "-qam", "rename")
    new, already = new_findings(repo, "main")
    assert new == [] and already >= 1


def test_a_second_copy_of_an_existing_finding_is_new_and_the_changed_line_is_blamed(repo):
    src = OLD + textwrap.dedent('''
        def classify_again(text):
            return client.chat.completions.create(model="gpt-4o-mini", max_tokens=5, messages=[])
    ''')
    (repo / "app" / "classify.py").write_text(src)
    new, _ = new_findings(repo, "main")
    # AIE001 reports each distinct id once per file, so a repeat of the same id is not
    # a second finding. A second unbounded call would be; this one is bounded.
    assert new == []
    (repo / "app" / "classify.py").write_text(src.replace("max_tokens=5, messages=[])", "messages=[])"))
    new, _ = new_findings(repo, "main")
    assert codes(new) == [("AIE002", "app/classify.py")]
    line = int(new[0].where.rsplit(":", 1)[1])
    assert "classify_again" in (repo / "app" / "classify.py").read_text().splitlines()[line - 2]


def test_unknown_ref_explains_the_ci_fix(repo):
    with pytest.raises(RefError, match="fetch-depth: 0"):
        new_findings(repo, "origin/does-not-exist")


def test_no_worktree_is_left_behind(repo):
    new_findings(repo, "main")
    assert git(repo, "worktree", "list").strip().count("\n") == 0


def test_cli_exit_codes(repo):
    run = lambda *a: subprocess.run([sys.executable, "-m", "journeyman.cli", "review", "--repo", str(repo), *a],
                                    cwd=ROOT, capture_output=True, text=True, timeout=120)
    (repo / "tests" / "test_triage.py").unlink()        # introduces AIE008, severity 85
    assert run("--fail-on", "80").returncode == 1
    r = run("--new-since", "main", "--fail-on", "80")
    assert r.returncode == 1 and "introduced since main" in r.stdout and "already there" in r.stdout
    (repo / "tests" / "test_triage.py").write_text('PROMPT = "prompts/triage_prompt.txt"\n')
    assert run("--new-since", "main", "--fail-on", "50").returncode == 0     # the old AIE001 does not count
    assert run("--fail-on", "50").returncode == 1                            # without the flag it does
    assert run("--new-since", "nope").returncode == 2


def test_workflow_passes_the_base_branch_only_on_pull_requests(tmp_path):
    yaml = pytest.importorskip("yaml")
    path, _ = init_workflow(tmp_path)
    doc = yaml.safe_load(path.read_text())
    steps = doc["jobs"]["review"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    run = next(s["run"] for s in steps if s.get("name", "").endswith("(inline annotations)"))
    assert run.startswith("journeyman review --format github --fail-on 80 ${{ github.event_name == 'pull_request'")
    assert "format('--new-since origin/{0}', github.base_ref)" in run
