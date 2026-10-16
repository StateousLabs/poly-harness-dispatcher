import urllib.request

import pytest

from helpers import make_tar, make_zip
from phd.errors import StackFetchError
from phd.stacks import sources
from phd.stacks.manifest import ComponentSource
from phd.stacks.sources import display_source, fetch


def test_display_source_keeps_ipv6_brackets_and_drops_the_query():
    assert display_source("https://[::1]:8080/a/b.zip?sig=SECRET#frag") == "https://[::1]:8080/a/b.zip"


def test_display_source_drops_an_invalid_port():
    assert display_source("https://host.example:99999999/x?sig=SECRET") == "https://host.example/x"


def test_origin_with_an_invalid_port_has_no_port():
    assert sources._origin("http://host.example:99999999/x") == ("http", "host.example", None)
    assert sources._origin("HTTPS://Host.Example/x") == ("https", "host.example", 443)


def test_local_archive_file_is_unpacked(tmp_path):
    src = tmp_path / "src"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "SKILL.md").write_text("x", encoding="utf-8")
    archive = make_tar(src, tmp_path / "pkg-1.0.tar.gz")
    f = fetch(ComponentSource(str(archive)), tmp_path / "work", what="skills[0]")
    assert (f.path / "SKILL.md").is_file() and f.name == "inner"  # unwrapped single top folder
    assert f.digest.startswith("sha256:") and f.path.is_relative_to(tmp_path / "work")


@pytest.mark.parametrize("exc", [OSError("secret-token"), ValueError("secret-token")])
def test_url_oserror_and_valueerror_never_echo_the_message(tmp_path, monkeypatch, exc):
    def boom(self, *a, **kw):
        raise exc
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    with pytest.raises(StackFetchError) as e:
        fetch(ComponentSource("http://127.0.0.1:1/x.zip?sig=SECRET"), tmp_path / "work", what="agent")
    msg = str(e.value)
    assert f"request failed ({type(exc).__name__})" in msg and "agent:" in msg
    assert "secret-token" not in msg and "SECRET" not in msg


def test_extensionless_tar_with_several_entries_is_sniffed(tmp_path, http_server):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "b.txt").write_text("b", encoding="utf-8")
    make_tar(src, http_server.www / "blob")
    f = fetch(ComponentSource(f"{http_server.url}/blob"), tmp_path / "work", what="skills[0]")
    assert f.name == "blob" and sorted(p.name for p in f.path.iterdir()) == ["a.txt", "b.txt"]


def test_extensionless_zip_is_sniffed(tmp_path, http_server):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "b.txt").write_text("b", encoding="utf-8")
    make_zip(src, http_server.www / "blob")
    f = fetch(ComponentSource(f"{http_server.url}/blob"), tmp_path / "work", what="skills[0]")
    assert f.name == "blob" and sorted(p.name for p in f.path.iterdir()) == ["a.txt", "b.txt"]
