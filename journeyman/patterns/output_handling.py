"""AIE015. Model output, or an argument the model chose, executed as code, shell or SQL.

A model's output is text that anyone who can influence its input can shape: the
user, a web page it read, a document it retrieved. Passing that text to eval, a
shell, or a SQL string gives all of them the same access the process has.

The version of this that agent code gets wrong most often is a tool: a function
decorated as a tool has its arguments filled in by the model, so
`@tool def run(cmd): subprocess.run(cmd, shell=True)` is a remote shell for
whoever controls the model's context.

Precision over recall, as everywhere in the reviewer. A value counts as model
output only when it is traced, inside one function, to a recognised model call
or to a tool function's parameter. A sink counts only when the traced value
reaches the dangerous position: the code string of eval/exec, the command of a
shell, the query text (not the bound parameters) of execute().
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .smells import Finding, _call_name

MODEL_CALL_SUFFIXES = (
    "chat.completions.create", "chat.completions.parse", "responses.create", "messages.create",
    "invoke_model", "converse", "generate_content", "litellm.completion", "ollama.chat",
    "ollama.generate",
)
LANGCHAIN_RECEIVERS = re.compile(r"(^|\.)(llm|chat|model|chain|agent|runnable)\w*$", re.I)
TOOL_DECORATORS = {"tool", "function_tool", "strands.tool", "langchain.tools.tool",
                   "langchain_core.tools.tool", "mcp.tool", "server.tool", "app.tool"}
SANITIZERS = {"shlex.quote", "ast.literal_eval", "int", "float", "bool", "len"}
SHELL_FUNCS = {"os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput"}
SUBPROCESS = {"subprocess.run", "subprocess.call", "subprocess.check_call",
              "subprocess.check_output", "subprocess.Popen"}


def _is_model_call(node: ast.AST) -> bool:
    if not isinstance(node, (ast.Call, ast.Await)):
        return False
    call = node.value if isinstance(node, ast.Await) else node
    if not isinstance(call, ast.Call):
        return False
    name = _call_name(call)
    if any(name == s or name.endswith("." + s) for s in MODEL_CALL_SUFFIXES):
        return True
    if name.endswith((".invoke", ".ainvoke", ".predict", ".run")) and \
            LANGCHAIN_RECEIVERS.search(name.rsplit(".", 1)[0]):
        return True
    return False


def _decorator_name(d: ast.AST) -> str:
    if isinstance(d, ast.Call):
        d = d.func
    parts = []
    while isinstance(d, ast.Attribute):
        parts.append(d.attr)
        d = d.value
    if isinstance(d, ast.Name):
        parts.append(d.id)
    return ".".join(reversed(parts))


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class _Flow:
    """Which local names hold model-controlled text, in one function."""

    def __init__(self, fn: ast.AST, tool_params: set[str]):
        self.tainted = set(tool_params)
        self.origin = {p: "tool argument" for p in tool_params}
        for _ in range(3):             # loops can carry taint backwards; a few passes settle it
            for node in ast.walk(fn):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    value = node.value
                    if value is None:
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    source = self.source(value)
                    if source:
                        for t in targets:
                            for n in ast.walk(t):
                                if isinstance(n, ast.Name) and n.id not in self.tainted:
                                    self.tainted.add(n.id)
                                    self.origin[n.id] = source
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                    pairs = [(node.target, node.iter)] if isinstance(node, (ast.For, ast.AsyncFor)) else \
                        [(i.optional_vars, i.context_expr) for i in node.items if i.optional_vars]
                    for target, value in pairs:
                        source = self.source(value)
                        if source:
                            for n in ast.walk(target):
                                if isinstance(n, ast.Name) and n.id not in self.tainted:
                                    self.tainted.add(n.id)
                                    self.origin[n.id] = source

    def source(self, value: ast.AST) -> str | None:
        """Why this expression is model-controlled, or None."""
        if _is_model_call(value):
            return "model response"
        for n in ast.walk(value):
            if isinstance(n, ast.Call) and _call_name(n) in SANITIZERS:
                if not (_names(n) - self._args_names(n)) & self.tainted:
                    return None if not (_names(value) - _names(n)) & self.tainted else self._first(value)
            if _is_model_call(n):
                return "model response"
        return self._first(value)

    def _args_names(self, call: ast.Call) -> set[str]:
        out = set()
        for a in [*call.args, *(k.value for k in call.keywords)]:
            out |= _names(a)
        return out

    def _first(self, value: ast.AST) -> str | None:
        hit = sorted(_names(value) & self.tainted)
        return self.origin.get(hit[0], "model response") if hit else None

    def reaches(self, expr: ast.AST | None) -> str | None:
        if expr is None:
            return None
        if _is_model_call(expr):
            return "model response"
        for n in ast.walk(expr):
            if isinstance(n, ast.Call) and _call_name(n) in SANITIZERS:
                return None if not (_names(expr) - _names(n)) & self.tainted else self._first(expr)
        return self._first(expr)


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _truthy(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _sink(call: ast.Call, flow: _Flow) -> tuple[str, str] | None:
    """(what, origin) when the model-controlled value lands in a dangerous position."""
    name = _call_name(call)
    first = call.args[0] if call.args else None
    if name in ("eval", "exec", "compile", "builtins.eval", "builtins.exec"):
        origin = flow.reaches(first)
        return (f"{name}()", origin) if origin else None
    if name in SHELL_FUNCS:
        origin = flow.reaches(first)
        return (f"{name}()", origin) if origin else None
    if name in SUBPROCESS and _truthy(_kw(call, "shell")):
        origin = flow.reaches(first or _kw(call, "args"))
        return (f"{name}(..., shell=True)", origin) if origin else None
    # conn.cursor().execute(sql) has no dotted name to match, only "execute"
    if isinstance(call.func, ast.Attribute) and call.func.attr in ("execute", "executemany", "executescript") \
            and first is not None:
        query = first.args[0] if isinstance(first, ast.Call) and _call_name(first) in ("text", "sqlalchemy.text") \
            and first.args else first
        origin = flow.reaches(query)
        return (f"{name.rsplit('.', 1)[-1]}() query text", origin) if origin else None
    return None


def check_model_output_executed(path: Path, text: str, tree: ast.AST, rel: str) -> list[Finding]:
    out: list[Finding] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = any(_decorator_name(d) in TOOL_DECORATORS or _decorator_name(d).endswith(".tool")
                      for d in fn.decorator_list)
        params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
                  if a.arg not in ("self", "cls")} if is_tool else set()
        flow = _Flow(fn, params)
        if not flow.tainted and not any(_is_model_call(n) for n in ast.walk(fn)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            hit = _sink(node, flow)
            if not hit:
                continue
            what, origin = hit
            via_tool = origin == "tool argument"
            out.append(Finding(
                code="AIE015",
                title=(f"a tool runs its model-chosen argument through {what}" if via_tool
                       else f"model output reaches {what}"),
                why=("Whoever can influence the model's context (the user, a retrieved document, "
                     "a web page) chooses this text. " +
                     ("A tool's arguments are written by the model, so this tool gives them "
                      "the process's own access." if via_tool else
                      "Executing it gives them the same access this process has.")),
                fix=("Do not execute model text. For commands, map the model's choice onto a fixed "
                     "allowlist and pass an argv list without shell=True. For SQL, use a read-only "
                     "role, allow only SELECT on named tables, and bind values as parameters. For "
                     "data, parse with json.loads or ast.literal_eval, never eval."),
                where=f"{rel}:{node.lineno}", severity=90,
                evidence=ast.get_source_segment(text, node) or "",
            ))
    return out
