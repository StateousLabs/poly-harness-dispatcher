"""The claude `.credentials.json` copy (`claude_host_login`) exists in a run home only while a turn runs, and
`Spec.chrome` reaches the command line."""

import json
import sys

import pytest

from conftest import FAKE_HARNESS, STREAMS
from phd import Dispatcher, Inputs, RunStatus
from phd.execution import credential_copies
from phd.spec import Credentials

FAKE = [sys.executable, str(FAKE_HARNESS)]
LOGIN = b'{\n  "claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}\n}\n'  # LF on purpose


@pytest.fixture(autouse=True)
def fresh_sweep_registry():
    credential_copies._SWEPT.clear()
    yield
    credential_copies._SWEPT.clear()


@pytest.fixture
def login_file(tmp_path):
    path = tmp_path / "source-credentials.json"
    path.write_bytes(LOGIN)
    return path


@pytest.fixture
def echo(tmp_path, monkeypatch):
    path = tmp_path / "echo.jsonl"
    monkeypatch.setenv("FAKE_ECHO_FILE", str(path))
    monkeypatch.setenv("FAKE_CAPTURE", ".credentials.json")
    monkeypatch.setenv("FAKE_STREAM", str(STREAMS / "claude_ok.jsonl"))
    return lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def dispatcher(spec, tmp_path):
    return Dispatcher(spec, runs_root=tmp_path / "runs", validate_model=False, allow_temp_runs_root=True,
                      executables={"claude": FAKE})


def test_the_turn_sees_the_exact_login_bytes_and_none_is_left_behind(tmp_path, make_spec, login_file, echo):
    spec = make_spec("claude", credentials=Credentials.claude_host_login(login_file))
    r = dispatcher(spec, tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED and r.credential_kind == "claude_host_login"
    assert bytes.fromhex(echo()[0]["captured"]) == LOGIN
    assert sorted((tmp_path / "runs").glob("*/home/.credentials.json")) == []
    assert login_file.read_bytes() == LOGIN  # the host's file is never touched


def test_chrome_reaches_the_command_line(tmp_path, make_spec, login_file, echo):
    spec = make_spec("claude", credentials=Credentials.claude_host_login(login_file), chrome=True)
    r = dispatcher(spec, tmp_path).spawn(Inputs(msg="x"))
    assert r.status is RunStatus.SUCCEEDED
    assert "--chrome" in echo()[0]["argv"]


def test_a_dead_runs_login_copy_is_swept(tmp_path, make_spec, login_file):
    home = tmp_path / "runs" / "dead-run" / "home"
    home.mkdir(parents=True)
    (home / ".credentials.json").write_bytes(LOGIN)
    spec = make_spec("claude", credentials=Credentials.claude_host_login(login_file))
    dispatcher(spec, tmp_path).plan(Inputs(msg="x"))
    assert not (home / ".credentials.json").exists()
