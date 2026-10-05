import io
import tarfile
import zipfile

import pytest

from helpers import make_tar, make_zip
from phd.errors import StackFetchError, StackInvalid
from phd.stacks.manifest import ComponentSource
from phd.stacks.sources import display_source, fetch


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "pdf"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pdf\ndescription: x\n---\n", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    return d


def test_local_folder(tmp_path, skill_dir):
    f = fetch(ComponentSource(str(skill_dir)), tmp_path / "work", what="skills[0]")
    assert f.path == skill_dir and f.name == "pdf" and f.digest.startswith("sha256:")


def test_local_file(tmp_path):
    md = tmp_path / "AGENTS.md"
    md.write_text("# me", encoding="utf-8")
    f = fetch(ComponentSource(str(md)), tmp_path / "work", what="agent")
    assert f.path == md and f.name == "AGENTS"


def test_local_missing(tmp_path):
    with pytest.raises(StackFetchError, match="skills\[2\]: .* does not exist"):
        fetch(ComponentSource(str(tmp_path / "nope")), tmp_path / "work", what="skills[2]")


@pytest.mark.parametrize("maker, top", [(make_tar, None), (make_tar, "pdf-2.1"), (make_zip, None), (make_zip, "pdf-2.1")])
def test_url_archives(tmp_path, http_server, skill_dir, maker, top):
    ext = ".tar.gz" if maker is make_tar else ".zip"
    maker(skill_dir, http_server.www / f"pdf-2.1{ext}", top=top)
    f = fetch(ComponentSource(f"{http_server.url}/pdf-2.1{ext}"), tmp_path / "work", what="skills[0]")
    assert (f.path / "SKILL.md").is_file() and (f.path / "scripts" / "run.py").is_file()
    assert f.name == "pdf-2.1"


def test_url_single_file(tmp_path, http_server):
    (http_server.www / "github.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    f = fetch(ComponentSource(f"{http_server.url}/github.json"), tmp_path / "work", what="mcp[0]")
    assert f.path.name == "github.json" and f.name == "github"


def test_headers_and_401(tmp_path, http_server, monkeypatch):
    monkeypatch.delenv("STACKS_TOKEN", raising=False)
    (http_server.www / "a.json").write_text("{}", encoding="utf-8")
    http_server.state["token"] = "sekrit"
    src = ComponentSource(f"{http_server.url}/a.json", headers={"Authorization": "Bearer ${env:STACKS_TOKEN}"})
    with pytest.raises(StackInvalid, match="STACKS_TOKEN"):
        fetch(src, tmp_path / "w1", what="mcp[0]")
    monkeypatch.setenv("STACKS_TOKEN", "wrong")
    with pytest.raises(StackFetchError, match="HTTP 401") as e:
        fetch(src, tmp_path / "w2", what="mcp[0]")
    assert "wrong" not in str(e.value)
    monkeypatch.setenv("STACKS_TOKEN", "sekrit")
    assert fetch(src, tmp_path / "w3", what="mcp[0]").path.name == "a.json"


def test_404(tmp_path, http_server):
    with pytest.raises(StackFetchError, match="HTTP 404"):
        fetch(ComponentSource(f"{http_server.url}/missing.tar.gz"), tmp_path / "work", what="base")


def test_presigned_query_never_in_errors(tmp_path, http_server):  # Review Focus 1
    url = f"{http_server.url}/missing.zip?X-Amz-Signature=abc123secret"
    with pytest.raises(StackFetchError) as e:
        fetch(ComponentSource(url), tmp_path / "work", what="base")
    assert "abc123secret" not in str(e.value)
    assert display_source(url) == f"{http_server.url}/missing.zip"


def test_unsafe_tar_is_refused(tmp_path, http_server):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"x"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    (http_server.www / "evil.tar.gz").write_bytes(buf.getvalue())
    with pytest.raises(StackFetchError, match="outside"):
        fetch(ComponentSource(f"{http_server.url}/evil.tar.gz"), tmp_path / "work", what="skills[0]")
    assert not (tmp_path / "evil.txt").exists()


def test_unsafe_zip_is_refused(tmp_path, http_server):
    with zipfile.ZipFile(http_server.www / "evil.zip", "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(StackFetchError, match="outside"):
        fetch(ComponentSource(f"{http_server.url}/evil.zip"), tmp_path / "work", what="skills[0]")


def test_corrupt_archive(tmp_path, http_server):
    (http_server.www / "bad.zip").write_bytes(b"not a zip")
    with pytest.raises(StackFetchError, match="not a usable archive"):
        fetch(ComponentSource(f"{http_server.url}/bad.zip"), tmp_path / "work", what="skills[0]")
