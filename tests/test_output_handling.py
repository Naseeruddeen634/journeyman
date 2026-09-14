"""AIE015: model output, or a tool argument the model chose, executed as code, shell or SQL."""

import ast
import textwrap
from pathlib import Path

from journeyman.patterns.output_handling import check_model_output_executed


def findings(src: str, rel: str = "app/agent.py"):
    text = textwrap.dedent(src)
    return check_model_output_executed(Path(rel), text, ast.parse(text), rel)


def test_exec_of_completion_content():
    f = findings("""
        from openai import OpenAI
        client = OpenAI()
        def run(task):
            resp = client.chat.completions.create(model=M, max_tokens=500, messages=[{"role": "user", "content": task}])
            code = resp.choices[0].message.content
            exec(code)
    """)
    assert [x.code for x in f] == ["AIE015"] and f[0].title == "model output reaches exec()"
    assert f[0].severity == 90 and "exec(code)" in f[0].evidence


def test_shell_through_an_fstring_and_os_system():
    f = findings("""
        import os, subprocess
        async def fix(llm, error):
            reply = await llm.ainvoke(f"suggest a shell command to fix: {error}")
            cmd = reply.content.strip()
            subprocess.run(f"cd /srv && {cmd}", shell=True, check=True)
            os.system(cmd)
    """)
    assert [x.title for x in f] == ["model output reaches subprocess.run(..., shell=True)",
                                   "model output reaches os.system()"]


def test_a_tool_that_runs_its_argument_in_a_shell():
    f = findings("""
        import subprocess
        from strands import tool
        @tool
        def run_shell(command: str) -> str:
            \"\"\"Run a shell command.\"\"\"
            return subprocess.run(command, shell=True, capture_output=True, text=True).stdout
    """)
    assert len(f) == 1 and f[0].title == "a tool runs its model-chosen argument through subprocess.run(..., shell=True)"
    assert "arguments are written by the model" in f[0].why


def test_text_to_sql_executes_the_query_text():
    f = findings("""
        import anthropic
        def answer(conn, question, client):
            msg = client.messages.create(model=M, max_tokens=300, messages=[{"role": "user", "content": question}])
            sql = msg.content[0].text
            rows = conn.cursor().execute(sql).fetchall()
            return rows
    """)
    assert [x.title for x in f] == ["model output reaches execute() query text"]


def test_safe_handling_is_silent():
    assert findings("""
        import ast, json, shlex, subprocess
        from strands import tool
        def parse(client):
            resp = client.chat.completions.create(model=M, max_tokens=50, messages=[])
            text = resp.choices[0].message.content
            data = json.loads(text)
            value = ast.literal_eval(text)
            subprocess.run(["grep", "-r", text, "."], check=True)          # argv, no shell
            subprocess.run("ls " + shlex.quote(text), shell=True)           # quoted
            print(text)
            return data, value
        def lookup(conn, client, q):
            resp = client.chat.completions.create(model=M, max_tokens=50, messages=[])
            name = resp.choices[0].message.content
            return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()   # bound
        @tool
        def list_dir(path: str) -> str:
            \"\"\"List a directory.\"\"\"
            return subprocess.run(["ls", path], capture_output=True, text=True).stdout
    """) == []


def test_unrelated_eval_and_shell_are_not_model_output():
    assert findings("""
        import subprocess
        def build(target):
            subprocess.run(f"make {target}", shell=True)
            return eval("1 + 1")
    """) == []


def test_journeymans_own_tools_do_not_trip_it():
    root = Path(__file__).resolve().parents[1] / "journeyman"
    hits = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf8")
        hits += check_model_output_executed(path, text, ast.parse(text), str(path.relative_to(root)))
    assert hits == [], [h.where for h in hits]
