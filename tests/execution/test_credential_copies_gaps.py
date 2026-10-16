"""Failure paths of the credential-copy sweep and cleanup."""

import os

import pytest

from phd.execution import credential_copies


@pytest.fixture(autouse=True)
def fresh_sweep_state():
    credential_copies._SWEPT.clear()
    yield
    credential_copies._SWEPT.clear()


def test_deleting_an_already_missing_copy_is_not_a_problem(tmp_path):
    assert credential_copies.drop([tmp_path / "auth.json"]) == []


def test_drop_reports_a_copy_that_cannot_be_deleted(tmp_path, monkeypatch):
    target = tmp_path / "auth.json"
    target.write_text("x")

    def refuse(path):
        raise PermissionError("in use")
    monkeypatch.setattr(os, "unlink", refuse)
    monkeypatch.setattr(credential_copies, "retry_sharing", lambda fn, **k: fn())
    problems = credential_copies.drop([target])
    assert len(problems) == 1 and "could not delete the credential copy" in problems[0] and "in use" in problems[0]


def test_an_entry_that_blows_up_is_reported_and_the_sweep_goes_on(tmp_path):
    root = tmp_path / "runs"
    for name in ("a", "b"):
        (root / name / "home").mkdir(parents=True)
    seen = []

    def secret_paths(home):
        seen.append(home.parent.name)
        raise RuntimeError("odd entry")

    with pytest.warns(RuntimeWarning, match="credential sweep skipped .*odd entry") as record:
        credential_copies.sweep_once(root, secret_paths)
    assert sorted(seen) == ["a", "b"] and len(record) == 2  # the first failure did not stop the second entry


def test_a_lock_that_cannot_be_released_is_warned_about_and_the_copy_still_deleted(tmp_path, monkeypatch):
    home = tmp_path / "runs" / "r1" / "home"
    home.mkdir(parents=True)
    copy = home / "auth.json"
    copy.write_text("secret")

    def failing_release(self):
        raise OSError("lock vanished")
    monkeypatch.setattr(credential_copies.SessionLock, "release", failing_release)
    with pytest.warns(RuntimeWarning, match="could not release the sweep's lock .*lock vanished"):
        credential_copies.sweep_once(tmp_path / "runs", lambda h: [h / "auth.json"])
    assert not copy.exists()
