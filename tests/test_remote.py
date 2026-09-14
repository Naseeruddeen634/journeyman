"""The AgentCore-facing service: fetch safely, analyse deterministically, read only inside the repo."""

import io
import tarfile
import textwrap
from pathlib import Path

import pytest

from journeyman.remote import RequestError, extract, handle, pack, parse_repo, read_tool

APP = textwrap.dedent('''
    from openai import OpenAI
    client = OpenAI()
    def triage(text):
        return client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": text}])
''')


def tarball(files: dict[str, str], top: str = "owner-name-abc123") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{rel}" if top else rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_only_public_github_urls_are_accepted():
    r = parse_repo("https://github.com/acme/helpdesk-ai")
    assert (r.owner, r.name, r.ref) == ("acme", "helpdesk-ai", "HEAD")
    assert parse_repo("https://github.com/acme/app.git").name == "app"
    assert parse_repo("https://github.com/acme/app/tree/feature/x").ref == "feature/x"
    assert r.archive_url == "https://codeload.github.com/acme/helpdesk-ai/tar.gz/HEAD"
    for bad in ("http://github.com/a/b", "https://github.com.evil.com/a/b", "https://169.254.169.254/latest",
                "file:///etc/passwd", "https://gitlab.com/a/b", ""):
        with pytest.raises(RequestError):
            parse_repo(bad)


def test_an_archive_that_escapes_its_directory_is_refused(tmp_path):
    evil = tarball({"../../escape.txt": "x"}, top="")
    with pytest.raises(Exception):
        extract(evil, tmp_path)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_review_mode_is_deterministic_and_needs_no_model(tmp_path):
    archive = tarball({"app/triage.py": APP, "prompts/triage_prompt.txt": "You are a triage assistant."})
    fetched = []

    def fetcher(repo, dest):
        fetched.append(repo.archive_url)
        return extract(archive, dest)

    out = handle({"repo": "https://github.com/acme/helpdesk-ai", "mode": "review"}, fetcher=fetcher)
    codes = sorted({f["code"] for f in out["findings"]})
    assert {"AIE001", "AIE002", "AIE008"} <= set(codes)
    assert out["inventory"]["sites"][0]["destination"] == "api.openai.com"
    assert "explanation" not in out and fetched == ["https://codeload.github.com/acme/helpdesk-ai/tar.gz/HEAD"]


def test_bad_mode_is_a_clear_error():
    with pytest.raises(RequestError, match="mode"):
        handle({"repo": "https://github.com/a/b", "mode": "fix-everything"}, fetcher=lambda r, d: d)


def test_the_agent_can_only_read_inside_the_repository(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "x.py").write_text("a = 1\nb = 2\n")
    (tmp_path / "secret.txt").write_text("do not read")
    read = read_tool(root)
    assert read(path="app/x.py") == "1: a = 1\n2: b = 2"
    assert "not a file" in read(path="../secret.txt")
    assert "not a file" in read(path="/etc/passwd")


def test_a_local_repository_can_be_sent_as_an_archive(tmp_path):
    import subprocess

    repo = tmp_path / "helpdesk-ai"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "triage.py").write_text(APP)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=repo, check=True)
    (repo / "uncommitted.py").write_text("SECRET = 1\n")          # only HEAD is sent

    out = handle({"archive": pack(repo), "name": "helpdesk-ai", "mode": "review"},
                 fetcher=lambda r, d: pytest.fail("must not download"))
    assert out["repo"] == "helpdesk-ai" and out["ref"] == "archive"
    assert any(f["code"] == "AIE001" and f["where"].startswith("app/triage.py") for f in out["findings"])
    assert not any("uncommitted" in f["where"] for f in out["findings"])


def test_a_bad_archive_is_a_clear_error():
    with pytest.raises(RequestError, match="base64"):
        handle({"archive": "not base64!!", "mode": "review"})
    with pytest.raises(RequestError, match="unpack"):
        handle({"archive": "aGVsbG8=", "mode": "review"})
