"""Catalogue edge cases: unknown harnesses, the static-check guard, and the CLI runner's failure paths."""

import subprocess
import sys

import pytest

from phd.errors import UnknownHarness
from phd.harnesses import catalog
from phd.harnesses.catalog import ModelCatalog


def test_unknown_harness_has_no_loader():
    with pytest.raises(UnknownHarness, match="gemini"):
        ModelCatalog(lambda argv: "")._load("gemini", lambda argv: "")
    with pytest.raises(UnknownHarness):
        ModelCatalog(lambda argv: "").models("gemini")


def test_static_catalogue_must_never_run_a_command():
    with pytest.raises(AssertionError, match="static catalogue ran a command"):
        catalog._no_command(["claude", "models"])


def test_run_cli_reports_a_failing_command_with_its_stderr(tmp_path):
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('boom from the cli'); sys.exit(3)"]
    with pytest.raises(RuntimeError, match=r"exited 3: boom from the cli"):
        catalog._run_cli(argv, env={}, cwd=tmp_path)


def test_run_cli_returns_stdout_on_success(tmp_path):
    argv = [sys.executable, "-c", "print('hello')"]
    assert catalog._run_cli(argv, env={}, cwd=tmp_path).strip() == "hello"


def test_run_cli_timeout_survives_a_process_that_will_not_die(monkeypatch, tmp_path):
    killed = []

    class Stuck:
        returncode = None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("cmd", timeout)

    monkeypatch.setattr(catalog, "resolve_argv", lambda argv: argv)  # codex need not be installed (CI)
    monkeypatch.setattr(catalog.subprocess, "Popen", lambda *a, **kw: Stuck())
    monkeypatch.setattr(catalog, "kill_tree", killed.append)
    # the second wait (after the kill) also times out: it is swallowed and the original timeout is re-raised
    with pytest.raises(subprocess.TimeoutExpired):
        catalog._run_cli(["codex", "debug", "models"], env={}, cwd=tmp_path)
    assert len(killed) == 1


def test_all_secret_paths_lists_only_codex_credential_copies(tmp_path):
    from phd.harnesses import all_secret_paths
    assert all_secret_paths(tmp_path) == [tmp_path / "auth.json"]
