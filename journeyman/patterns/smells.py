"""What a senior AI engineer catches in review.

Generic linters find unused imports. None of them know that a prompt with no
eval is a liability, that a hardcoded model id is a deprecation waiting to
happen, or that `json.loads(response.content)` is going to page someone at 3am.

This is that knowledge, written down as checks. Each one is something a person
who has shipped LLM features would stop at in a pull request, with the reason
they would give and the fix they would suggest.

Every check is static and deterministic. No model is asked whether your code is
good; these are patterns, and patterns are matchable. That keeps the findings
identical on every machine and makes each one arguable, which is the point of a
review.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".journeyman",
        "dist", "build", ".pytest_cache", "runs", ".mypy_cache"}


@dataclass
class Finding:
    code: str               # short stable id, e.g. AIE001
    title: str
    why: str                # what goes wrong, concretely
    fix: str                # what to do instead
    where: str              # file:line
    severity: int           # 90 breaks in prod, 60 costs money, 40 tech debt
    evidence: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "title": self.title, "why": self.why,
                "fix": self.fix, "where": self.where, "severity": self.severity,
                "evidence": self.evidence[:400]}


# Signals that a file is doing LLM work at all. Checks below only fire inside
# these files, which is what keeps the false-positive rate survivable.
LLM_MARKERS = re.compile(
    r"\b(openai|anthropic|bedrock|ollama|litellm|langchain|llama_index|llamaindex|"
    r"strands|mistral|cohere|together|groq|huggingface|transformers|"
    r"chat\.completions|messages\.create|invoke_model|generate_content|"
    r"ChatPromptTemplate|system_prompt|completion\()",
    re.I,
)

MODEL_ID = re.compile(
    r"[\"']((?:gpt|o[13]|claude|gemini|llama|mistral|qwen|deepseek|command|kimi)"
    r"[\w.\-]*(?:turbo|opus|sonnet|haiku|mini|pro|flash|instruct|preview|\d[\w.\-]*))[\"']",
    re.I,
)


def _files(repo: Path) -> list[Path]:
    return [p for p in repo.rglob("*.py") if not any(s in p.parts for s in SKIP)]


def _is_llm_file(text: str) -> bool:
    return bool(LLM_MARKERS.search(text))


# ------------------------------------------------------------------ checks


def check_hardcoded_model_ids(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE001. A model id buried in code is a deprecation with a delay fuse."""
    out = []
    seen = set()
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = MODEL_ID.search(line)
        if not m or m.group(1) in seen:
            continue
        # A module-level constant or an env lookup is the correct pattern, so
        # skip those. Match the constant at the start of a line: an earlier
        # version matched `MODEL\s*=` anywhere, which silently skipped the
        # kwarg `model="gpt-4o-mini"` that this check exists to find.
        if re.search(r"^\s*[A-Z_]{3,}\s*=", line) or \
                re.search(r"(getenv|environ\[|environ\.get|Field\(|argparse)", line):
            continue
        seen.add(m.group(1))
        out.append(Finding(
            code="AIE001",
            title=f"model id {m.group(1)!r} is hardcoded",
            why=("Providers deprecate model ids on their own schedule. When this one "
                 "goes, the failure is a 404 at runtime in whatever environment hits "
                 "it first, and the id is in the middle of a function rather than "
                 "anywhere you would think to look."),
            fix=("Lift it to one module-level constant read from the environment, "
                 "e.g. MODEL = os.environ.get('APP_MODEL', 'a-known-good-default'). "
                 "One place to change, one place to grep."),
            where=f"{rel}:{i}", severity=60, evidence=line.strip()[:160],
        ))
    return out


def check_unbounded_output(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE002. No max_tokens is an unbounded bill and an unbounded latency."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if not re.search(r"(create|completion|invoke|generate|chat)$", name, re.I):
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if not ({"model", "messages", "prompt", "contents"} & kwargs):
            continue
        if {"max_tokens", "max_output_tokens", "maxTokens", "max_completion_tokens"} & kwargs:
            continue
        out.append(Finding(
            code="AIE002",
            title=f"{name}() has no max_tokens",
            why=("Output length is then whatever the model feels like. That is an "
                 "unbounded cost per call and an unbounded p99 latency, and it "
                 "usually shows up first as a timeout somewhere unrelated."),
            fix="Set max_tokens to the largest output you actually expect, plus headroom.",
            where=f"{rel}:{node.lineno}", severity=60,
        ))
    return out


def check_unvalidated_parsing(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE003. json.loads on model output is a crash waiting for a Tuesday."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) not in ("json.loads", "loads"):
            continue
        src = ast.get_source_segment(text, node) or ""
        if not re.search(r"(response|completion|message|content|output|result|text|reply)",
                         src, re.I):
            continue
        out.append(Finding(
            code="AIE003",
            title="model output parsed with json.loads and no schema",
            why=("Models emit prose around JSON, trailing commas, and code fences. "
                 "json.loads raises on all of it, and the traceback points at the "
                 "parse rather than at the prompt that caused it."),
            fix=("Use the provider's structured-output or tool-calling mode so the "
                 "shape is enforced, and validate with a Pydantic model. If you must "
                 "parse by hand, catch JSONDecodeError and keep the raw text in the error."),
            where=f"{rel}:{node.lineno}", severity=70, evidence=src[:160],
        ))
    return out


def check_prompt_injection_surface(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE004. User text interpolated into a prompt with no boundary."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not re.search(r"(prompt|system|instruction|template)", line, re.I):
            continue
        if not re.search(r'f["\']|\.format\(|%\s*\(|\+\s*\w+', line):
            continue
        # match the word anywhere in the placeholder: {ticket_text},
        # {customer_message} and {q} are all untrusted input
        # `\b` will not split ticket_text, because underscore counts as a word
        # character. Treat underscores as separators explicitly.
        if not re.search(
            r"\{[^}]*(?:^|[^A-Za-z])?(user|query|question|input|message|text|"
            r"content|body|comment|ticket|reply|answer|doc|q)"
            r"(?:_[a-z]+)*(?:[^A-Za-z}]|\}|$)", line, re.I
        ):
            continue
        out.append(Finding(
            code="AIE004",
            title="user input goes straight into a prompt",
            why=("Whatever the user types is now instructions. The classic result is "
                 "'ignore the above', but the quiet version is a user pasting a "
                 "document that happens to contain something imperative."),
            fix=("Put untrusted text inside a delimited block and say in the system "
                 "prompt that everything inside it is data, never instructions. Keep "
                 "the instructions in the system prompt, not the user turn."),
            where=f"{rel}:{i}", severity=80, evidence=line.strip()[:160],
        ))
    return out


def check_no_retry_or_timeout(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE005. An LLM call is a network call. It will fail."""
    has_guard = re.search(r"(retry|backoff|tenacity|timeout|max_retries)", text, re.I)
    if has_guard:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if not re.search(r"(chat\.completions\.create|messages\.create|invoke_model|"
                         r"generate_content|completion)$", name, re.I):
            continue
        out.append(Finding(
            code="AIE005",
            title=f"{name}() has no timeout or retry anywhere in this file",
            why=("Providers rate-limit, time out and return 529s under load. Without a "
                 "guard the first failure is an unhandled exception in whatever called "
                 "you, usually during your busiest hour."),
            fix=("Set an explicit timeout and retry on the transient classes only "
                 "(429, 5xx, connection errors) with exponential backoff. Do not "
                 "retry on 400s: those are your bug, and retrying hides it."),
            where=f"{rel}:{node.lineno}", severity=70,
        ))
        break   # one per file is enough to make the point
    return out


def check_swallowed_errors(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE006. A bare except around an LLM call hides the thing you need to see."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is not None and not (
            isinstance(node.type, ast.Name) and node.type.id == "Exception"
        ):
            continue
        body = ast.get_source_segment(text, node) or ""
        if not re.search(r"(pass|continue|return None|return \"\"|return \{\})", body):
            continue
        out.append(Finding(
            code="AIE006",
            title="an exception is swallowed silently in an LLM path",
            why=("A rate limit, a malformed response and a genuine bug now look "
                 "identical from the outside: an empty result. Quality drops and "
                 "nothing in the logs says why."),
            fix=("Catch the specific exceptions you expect, log the provider's error "
                 "body, and let anything else propagate."),
            where=f"{rel}:{node.lineno}", severity=65, evidence=body[:160],
        ))
    return out


def check_nondeterministic_tests(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE007. A test that calls a live model is not a test."""
    if not re.search(r"(^|/)(test_|tests/)", rel) and "test" not in Path(rel).name:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if not re.search(r"(chat\.completions\.create|messages\.create|invoke_model|"
                         r"generate_content)$", name, re.I):
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if "temperature" in kwargs or "seed" in kwargs:
            continue
        out.append(Finding(
            code="AIE007",
            title="a test calls a live model with no temperature or seed",
            why=("The suite is now flaky, slow and billed. A red build will not tell "
                 "you whether the code broke or the model had an off day, so people "
                 "learn to re-run it, which is how a real failure gets ignored."),
            fix=("Record the responses and replay them, or stub the provider. Keep "
                 "live calls in a separate, opt-in suite that does not gate a merge."),
            where=f"{rel}:{node.lineno}", severity=75,
        ))
    return out


def check_prompt_without_eval(repo: Path) -> list[Finding]:
    """AIE008. The one that matters most. A prompt nobody measures."""
    prompt_files: list[tuple[Path, str]] = []
    for p in repo.rglob("*"):
        if any(s in p.parts for s in SKIP) or not p.is_file():
            continue
        if p.suffix.lower() in {".txt", ".md", ".jinja", ".j2", ".prompt"} and \
                re.search(r"prompt", str(p), re.I):
            prompt_files.append((p, p.read_text(encoding="utf8", errors="ignore")))

    test_text = ""
    for d in ("tests", "test", "evals", "eval"):
        for p in (repo / d).rglob("*.py") if (repo / d).exists() else []:
            test_text += p.read_text(encoding="utf8", errors="ignore")

    out = []
    for p, _ in prompt_files:
        rel = str(p.relative_to(repo))
        if p.stem in test_text or rel in test_text:
            continue
        out.append(Finding(
            code="AIE008",
            title=f"{rel} is a prompt with no eval",
            why=("Nothing measures this prompt, so nobody can tell whether the next "
                 "edit helps or hurts. Changes get made on feel and regressions ship "
                 "silently, because there is no signal to go red."),
            fix=("Build a small eval set for it, hold half of it back, and record the "
                 "score before you touch it. Twenty cases beats zero by more than a "
                 "hundred beats twenty."),
            where=rel, severity=85,
        ))
    return out


def check_untracked_cost(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    """AIE009. Calls in a loop with no usage accounting."""
    if re.search(r"(usage|token_count|prompt_tokens|total_tokens|cost|budget)", text, re.I):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if re.search(r"(chat\.completions\.create|messages\.create|invoke_model|"
                         r"generate_content)$", _call_name(inner), re.I):
                out.append(Finding(
                    code="AIE009",
                    title="model calls inside a loop with no usage tracking",
                    why=("Cost scales with whatever that loop iterates over, and "
                         "nothing in the code knows how much it spent. The first "
                         "signal is the invoice."),
                    fix=("Accumulate usage from each response and log the total per "
                         "request. Add a hard cap that stops the loop rather than "
                         "trusting the input to be small."),
                    where=f"{rel}:{node.lineno}", severity=65,
                ))
                break
        if out:
            break
    return out


FILE_CHECKS = [
    check_hardcoded_model_ids, check_unbounded_output, check_unvalidated_parsing,
    check_prompt_injection_surface, check_no_retry_or_timeout, check_swallowed_errors,
    check_nondeterministic_tests, check_untracked_cost,
]


def _call_name(node: ast.Call) -> str:
    parts = []
    cur = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def review(repo: str | Path) -> list[Finding]:
    """Review a repository the way an AI engineer would, worst first."""
    repo = Path(repo).resolve()
    findings: list[Finding] = []

    for path in _files(repo):
        try:
            text = path.read_text(encoding="utf8", errors="ignore")
        except OSError:
            continue
        if not _is_llm_file(text):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = str(path.relative_to(repo))
        for check in FILE_CHECKS:
            try:
                findings.extend(check(path, text, tree, rel))
            except Exception:
                continue   # a broken check must never take the review down

    findings.extend(check_prompt_without_eval(repo))
    return sorted(findings, key=lambda f: -f.severity)


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "No AI engineering issues found."
    by_code: dict[str, int] = {}
    for f in findings:
        by_code[f.code] = by_code.get(f.code, 0) + 1
    return f"{len(findings)} finding(s) across {len(by_code)} check(s)"
