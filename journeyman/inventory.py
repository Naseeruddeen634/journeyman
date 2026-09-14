"""Every place this codebase talks to a model, and where the words go.

The first week on an AI team, and every security review after it, starts with
the same questions: which models do we call, from where, is the model id pinned
or configurable, is output bounded, which calls send text to a third party, and
which prompts does anyone measure. The answers are usually spread across a
dozen files and one person's memory.

This answers them statically: no model, no network, nothing executed.

What it can say and what it cannot:
  - A model id is reported as a literal, as an environment variable with its
    default, or as "dynamic" with the expression. It is never guessed.
  - Where the text goes is inferred from the provider and any base URL written in
    the file. A base URL that only exists in deployment configuration is
    reported as unknown rather than assumed to be the provider's default.
  - Test files are counted separately. A call in a test is usually a stub.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .patterns.smells import (MODEL_CONSTRUCTORS, OUTPUT_LIMIT_KWARGS, _call_name, _files,
                              _is_llm_file, _skipped, ignore_patterns, is_ignored, is_test_file)

# dotted-name suffix -> provider
SDK_CALLS = {
    "chat.completions.create": "openai-compatible",
    "chat.completions.parse": "openai-compatible",
    "responses.create": "openai-compatible",
    "embeddings.create": "openai-compatible",
    "messages.create": "anthropic",
    "messages.stream": "anthropic",
    "invoke_model": "bedrock",
    "invoke_model_with_response_stream": "bedrock",
    "converse": "bedrock",
    "converse_stream": "bedrock",
    "generate_content": "google",
    "litellm.completion": "litellm",
    "litellm.acompletion": "litellm",
    "ollama.chat": "ollama",
    "ollama.generate": "ollama",
}
CONSTRUCTOR_PROVIDER = {
    "OllamaModel": "ollama", "ChatOllama": "ollama",
    "BedrockModel": "bedrock", "ChatBedrock": "bedrock", "ChatBedrockConverse": "bedrock",
    "OpenAIModel": "openai-compatible", "ChatOpenAI": "openai-compatible",
    "AnthropicModel": "anthropic", "ChatAnthropic": "anthropic",
    "LiteLLMModel": "litellm",
}
assert set(CONSTRUCTOR_PROVIDER) == set(MODEL_CONSTRUCTORS)
MODEL_KWARGS = ("model", "model_id", "modelId", "model_name")
URL = re.compile(r"""["'](https?://[^"'\s]+)["']""")
LOCAL_URL = re.compile(r"//(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:|/|$)")


@dataclass
class Site:
    where: str
    kind: str              # the call or constructor, e.g. chat.completions.create
    provider: str
    model: str             # literal, "env VAR (default X)", or "dynamic: expr"
    model_source: str      # literal | env | constant | dynamic | provider default | none
    destination: str
    leaves_machine: bool | None
    output_limit: bool | None
    test: bool = False


@dataclass
class Prompt:
    path: str
    eval_status: str


@dataclass
class Inventory:
    repo: str
    sites: list[Site] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)

    def production(self) -> list[Site]:
        return [s for s in self.sites if not s.test]

    def to_dict(self) -> dict:
        return {"repo": self.repo, "sites": [asdict(s) for s in self.sites],
                "prompts": [asdict(p) for p in self.prompts]}


# ------------------------------------------------------------------ resolving


def _module_values(tree: ast.AST) -> dict[str, ast.AST]:
    out = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            out[node.target.id] = node.value
    return out


def _env_lookup(node: ast.AST) -> tuple[str, str | None] | None:
    """os.environ.get("X", "d") / os.getenv("X", "d") / os.environ["X"] -> (X, d)."""
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name.endswith(("environ.get", "getenv")) and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            default = node.args[1] if len(node.args) > 1 else None
            d = default.value if isinstance(default, ast.Constant) and isinstance(default.value, str) else None
            return node.args[0].value, d
    if isinstance(node, ast.Subscript) and _call_name_of(node.value).endswith("environ") and \
            isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value, None
    return None


def _call_name_of(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def resolve_model(node: ast.AST | None, module: dict[str, ast.AST]) -> tuple[str, str]:
    if node is None:
        return "", "none"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, "literal"
    env = _env_lookup(node)
    if env:
        return (f"env {env[0]}" + (f" (default {env[1]})" if env[1] else "")), "env"
    if isinstance(node, ast.Name) and node.id in module:
        value, source = resolve_model(module[node.id], {})
        if source == "literal":
            return f"{value} (constant {node.id})", "constant"
        if source == "env":
            return value, "env"
    try:
        return f"dynamic: {ast.unparse(node)[:60]}", "dynamic"
    except Exception:
        return "dynamic", "dynamic"


def _kwarg(node: ast.Call, *names: str) -> ast.AST | None:
    for k in node.keywords:
        if k.arg in names:
            return k.value
    for k in node.keywords:            # client_args={"base_url": ...}, params={"model": ...}
        if isinstance(k.value, ast.Dict):
            for key, value in zip(k.value.keys, k.value.values):
                if isinstance(key, ast.Constant) and key.value in names:
                    return value
    return None


def destination(provider: str, text: str, base_url: str | None = None) -> tuple[str, bool | None]:
    """Where the prompt text goes, and whether it leaves this machine."""
    urls = [base_url] if base_url else URL.findall(text)
    if provider == "ollama":
        remote = [u for u in urls if "11434" in u and not LOCAL_URL.search(u)]
        return ("ollama on " + remote[0], True) if remote else ("local (ollama)", False)
    if provider == "bedrock":
        return "AWS account (bedrock)", True
    if provider == "anthropic":
        return "api.anthropic.com", True
    if provider == "google":
        return "Google (Gemini API or Vertex)", True
    if provider == "litellm":
        return "depends on the model prefix (litellm)", None
    if provider == "openai-compatible":
        if base_url and LOCAL_URL.search(base_url):
            return f"local ({base_url})", False
        for u in urls:
            if "openrouter.ai" in u:
                return "openrouter.ai", True
            if LOCAL_URL.search(u):
                return f"local ({u})", False
        if re.search(r"\bbase_url\b|OPENAI_BASE_URL", text):
            return "configured base URL (not visible in code)", None
        return "api.openai.com", True
    return "unknown", None


def output_limit(node: ast.Call) -> bool | None:
    """True if a limit is visible anywhere in the call, None if it could be hidden."""
    keys = {k.arg for k in node.keywords if k.arg}
    for sub in ast.walk(node):          # params={...}, inferenceConfig={...}, body=json.dumps({...})
        if isinstance(sub, ast.Dict):
            keys |= {k.value for k in sub.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if keys & OUTPUT_LIMIT_KWARGS:
        return True
    if any(k.arg is None for k in node.keywords):
        return None                     # **config
    opaque = {"body", "inferenceConfig", "params", "options", "generation_config", "config",
              "model_kwargs", "additional_request_fields"}
    if any(k.arg in opaque and not isinstance(k.value, ast.Dict) for k in node.keywords):
        return None                     # built elsewhere; the limit may be in there
    return False


# ------------------------------------------------------------------ Python


def python_sites(rel: str, text: str) -> list[Site]:
    if not _is_llm_file(text):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    module = _module_values(tree)
    test = is_test_file(rel)
    out = []
    strands_agent = re.search(r"from\s+strands\s+import\s+[^\n]*\bAgent\b", text) is not None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        short = name.split(".")[-1]
        provider, kind = None, name
        if short in CONSTRUCTOR_PROVIDER:
            provider, kind = CONSTRUCTOR_PROVIDER[short], short
        else:
            for suffix, prov in SDK_CALLS.items():
                if name == suffix or name.endswith("." + suffix):
                    provider, kind = prov, suffix
                    break
            if provider == "anthropic" and not re.search(r"\banthropic\b", text, re.I):
                provider = None      # messages.create is also Twilio, Slack, ...
            if provider == "bedrock" and not re.search(r"bedrock", text, re.I):
                provider = None
            if provider is None and strands_agent and name == "Agent" and \
                    _kwarg(node, "model") is None:
                provider, kind = "bedrock", "Agent (strands default model)"
        if provider is None:
            continue

        model_node = _kwarg(node, *MODEL_KWARGS)
        model, source = resolve_model(model_node, module)
        if kind.startswith("Agent ("):
            model, source = "strands default (Claude on Bedrock)", "provider default"
        base = _kwarg(node, "base_url", "host")
        base_value, base_source = resolve_model(base, module)
        default_url = re.search(r"\(default (https?://\S+)\)", base_value)
        base_url = base_value if base_value.startswith("http") else \
            (default_url.group(1) if default_url else None)
        dest, leaves = destination(provider, text, base_url)
        if base_source == "env":
            dest += f", unless {base_value.split()[1]} is set"
        limit = None if kind.startswith("Agent (") else output_limit(node)
        out.append(Site(f"{rel}:{node.lineno}", kind, provider, model or "(not set here)",
                        source, dest, leaves, limit, test))
    return out


# ------------------------------------------------------------------ TypeScript

TS_CALL = re.compile(r"\b(chat\.completions\.create|responses\.create|embeddings\.create|"
                     r"messages\.create|generateText|streamText|generateObject|streamObject|"
                     r"embed|embedMany)\s*\(")
AI_SDK_FACTORY = re.compile(r"\bmodel\s*:\s*(openai|anthropic|google|bedrock|ollama|mistral|"
                            r"groq|azure|openrouter)(?:\.\w+)?\s*\(\s*")
TS_PROVIDER = {"openai": "openai-compatible", "azure": "openai-compatible", "anthropic": "anthropic",
               "google": "google", "bedrock": "bedrock", "ollama": "ollama",
               "openrouter": "openai-compatible", "mistral": "mistral", "groq": "groq"}


def typescript_sites(rel: str, src: str) -> list[Site]:
    from .patterns.smells_ts import LLM_MARKERS_TS, OUTPUT_LIMIT, _call_args, is_test_path, lex

    if not LLM_MARKERS_TS.search(src):
        return []
    lx = lex(src)
    literal_at = {t.start: t.text for t in lx.strings if t.kind == "quote"}
    out = []
    for m in TS_CALL.finditer(lx.masked):
        kind = m.group(1)
        if kind in ("embed", "embedMany") and "from \"ai\"" not in src and "from 'ai'" not in src:
            continue
        if kind == "messages.create" and "anthropic" not in src.lower():
            continue
        a, b = _call_args(lx, m.end() - 1)
        span = lx.masked[a:b]
        provider = {"messages.create": "anthropic"}.get(kind, "openai-compatible")
        model, source = "(not set here)", "none"
        fac = AI_SDK_FACTORY.search(span)
        if fac:
            provider = TS_PROVIDER.get(fac.group(1), fac.group(1))
            lit = literal_at.get(a + fac.end())
            model, source = (lit, "literal") if lit else ("dynamic", "dynamic")
            if fac.group(1) == "openrouter":
                dest, leaves = "openrouter.ai", True
        else:
            mm = re.search(r"\bmodel\s*:\s*(\S)", span)
            if mm:
                lit = literal_at.get(a + mm.start(1))
                if lit is not None:
                    model, source = lit, "literal"
                else:
                    expr = lx.code_only[a + mm.start(1): a + mm.start(1) + 60].split(",")[0].split("\n")[0]
                    env = re.search(r"process\.env\.(\w+)(?:\s*\?\?\s*['\"]([^'\"]+))?", expr)
                    model, source = ((f"env {env.group(1)}" + (f" (default {env.group(2)})" if env.group(2) else ""), "env")
                                     if env else (f"dynamic: {expr.strip()}", "dynamic"))
        if not (fac and fac.group(1) == "openrouter"):
            base = re.search(r"baseURL\s*:\s*['\"]([^'\"]+)", lx.code_only)
            dest, leaves = destination(provider, src, base.group(1) if base else None)
            if provider == "openai-compatible" and not base and re.search(r"baseURL", lx.code_only):
                dest, leaves = "configured base URL (not visible in code)", None
        limit = None if "..." in span else bool(OUTPUT_LIMIT.search(lx.code_only[a:b]))
        out.append(Site(f"{rel}:{lx.line_of(m.start())}", kind, provider, model, source, dest,
                        leaves, limit, is_test_path(rel)))
    return out


# ------------------------------------------------------------------ prompts


def prompt_eval_status(repo: Path) -> list[Prompt]:
    patterns = ignore_patterns(repo)
    tests_text = ""
    for d in ("tests", "test", "evals", "eval"):
        if (repo / d).exists():
            for p in (repo / d).rglob("*.py"):
                tests_text += p.read_text(encoding="utf8", errors="ignore")
    out = []
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or _skipped(p, repo):
            continue
        rel = str(p.relative_to(repo))
        if patterns and is_ignored(rel, patterns):
            continue
        if p.suffix.lower() not in {".txt", ".md", ".jinja", ".j2", ".prompt"} or \
                not re.search(r"prompt", rel, re.I) or rel.startswith("evals/"):
            continue
        name = re.sub(r"[^A-Za-z0-9_]+", "_", p.stem)
        cases = repo / "evals" / name / "cases.jsonl"
        baseline = repo / "evals" / name / "baseline.json"
        if cases.exists():
            n = sum(1 for line in cases.read_text(encoding="utf8").splitlines() if line.strip())
            status = f"eval: {n} cases"
            if baseline.exists():
                try:
                    held = json.loads(baseline.read_text(encoding="utf8"))["scores"].get("holdout")
                    status += f", holdout baseline {held:.0%}" if held is not None else ", no holdout score"
                except (json.JSONDecodeError, KeyError, TypeError):
                    status += ", baseline unreadable"
            else:
                status += ", never recorded"
        elif p.stem in tests_text or rel in tests_text:
            status = "referenced by tests"
        else:
            status = "no eval"
        out.append(Prompt(rel, status))
    return out


def build(repo: str | Path) -> Inventory:
    from .patterns.smells_ts import TS_SUFFIXES

    repo = Path(repo).resolve()
    inv = Inventory(str(repo))
    for path in _files(repo):
        try:
            inv.sites += python_sites(str(path.relative_to(repo)), path.read_text(encoding="utf8", errors="ignore"))
        except OSError:
            continue
    patterns = ignore_patterns(repo)
    for path in repo.rglob("*"):
        if path.suffix not in TS_SUFFIXES or path.name.endswith(".d.ts") or not path.is_file() \
                or _skipped(path, repo):
            continue
        rel = str(path.relative_to(repo))
        if patterns and is_ignored(rel, patterns):
            continue
        inv.sites += typescript_sites(rel, path.read_text(encoding="utf8", errors="ignore"))
    inv.sites.sort(key=lambda s: (s.test, s.where.rsplit(":", 1)[0], int(s.where.rsplit(":", 1)[1])))
    inv.prompts = prompt_eval_status(repo)
    return inv


def render(inv: Inventory) -> str:
    prod = inv.production()
    files = {s.where.rsplit(":", 1)[0] for s in prod}
    lines = [f"\n  Model inventory: {Path(inv.repo).name}",
             f"  {len(prod)} call site(s) in {len(files)} file(s)"
             + (f", plus {len(inv.sites) - len(prod)} in tests" if len(inv.sites) > len(prod) else "")]
    if not prod and not inv.prompts:
        lines.append("\n  No model calls found.\n")
        return "\n".join(lines)

    by_dest: dict[str, list[Site]] = {}
    for s in prod:
        by_dest.setdefault(s.destination, []).append(s)
    lines.append("\n  where the text goes")
    for dest, sites in sorted(by_dest.items(), key=lambda kv: (kv[1][0].leaves_machine is False, kv[0])):
        mark = {True: "leaves this machine", False: "stays local", None: "cannot tell from code"}[sites[0].leaves_machine]
        models = sorted({s.model for s in sites})
        lines.append(f"    {dest:<44} {len(sites):>3} site(s)  {mark}")
        for model in models[:4]:
            lines.append(f"      {model}")

    hard = [s for s in prod if s.model_source == "literal"]
    unbounded = [s for s in prod if s.output_limit is False]
    unknown = [s for s in prod if s.leaves_machine is None]
    lines.append("")
    lines.append(f"  {len(hard)} with the model id hardcoded at the call, "
                 f"{len(unbounded)} with no output limit, "
                 f"{len(unknown)} whose destination depends on configuration")

    lines.append("\n  call sites")
    for s in prod:
        limit = {True: "limit", False: "NO LIMIT", None: "limit ?"}[s.output_limit]
        lines.append(f"    {s.where:<40} {s.kind:<26} {limit:<9} {s.model[:48]}")
    if inv.prompts:
        lines.append("\n  prompt files")
        for p in inv.prompts:
            lines.append(f"    {p.path:<52} {p.eval_status}")
    return "\n".join(lines) + "\n"
