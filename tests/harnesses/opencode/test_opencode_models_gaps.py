import shutil

from phd.harnesses.opencode import models as opencode_models


def test_catalogue_session_without_opencode_credentials_sets_no_auth_content(phd_cache_dir):
    with opencode_models.catalog_session(None, "none", None, []) as (env, home):
        assert "OPENCODE_AUTH_CONTENT" not in env
        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and home.is_dir()


def test_failed_models_cache_copy_leaves_no_temp_file(monkeypatch, tmp_path):
    host = tmp_path / "hostcache" / "opencode"
    host.mkdir(parents=True)
    (host / "models.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "hostcache"))

    def broken_copy(src, dst):
        with open(dst, "w", encoding="utf-8") as f:
            f.write("partial")
        raise OSError("disk full")
    monkeypatch.setattr(shutil, "copy2", broken_copy)
    cache = tmp_path / "phd-cache"
    opencode_models.seed_models_cache(cache)  # must not raise
    assert not (cache / "opencode" / "models.json").exists()
    assert list((cache / "opencode").glob("*.tmp")) == []
