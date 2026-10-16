from phd.harnesses.codex import models as codex_models


def test_catalogue_session_without_credentials_has_no_auth_file(phd_cache_dir):
    notes = []
    with codex_models.catalog_session(None, "none", None, notes) as (env, home):
        assert env == {"CODEX_HOME": str(home)} and home.is_dir()
        assert not (home / "auth.json").exists()
    assert not home.exists() and notes == []


def test_catalogue_session_tolerates_a_home_that_is_already_gone(phd_cache_dir):
    notes = []
    with codex_models.catalog_session(None, "none", None, notes) as (_, home):
        home.rmdir()  # the command (or something else) removed it first
    assert notes == []
