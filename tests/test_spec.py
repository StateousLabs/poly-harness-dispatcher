import json

import pytest

from conftest import FIXTURES
from phd import placeholders
from phd.errors import (CwdNotFound, EmptyTask, InputNotFound, InvalidSpec, MissingCredentials,
                        MissingSecret, StackIncompatible, UnknownHarness, UnsafeAutonomy)
from phd.spec import Credentials, Inputs


def test_env_refs_and_resolve():
    value = {"args": ["${stack}/mcp/x.py"], "env": {"A": "${env:TOKEN_A}", "B": "pre-${env:B_1}"}}
    assert placeholders.env_refs(value) == {"TOKEN_A", "B_1"}
    out = placeholders.resolve(value, stack_dir="/run/home/stack", secrets={"TOKEN_A": "a", "B_1": "b"})
    assert out == {"args": ["/run/home/stack/mcp/x.py"], "env": {"A": "a", "B": "pre-b"}}


def test_resolve_missing_secret():
    with pytest.raises(MissingSecret, match="NOPE"):
        placeholders.resolve("${env:NOPE}", stack_dir="/s", secrets={})


def test_spec_valid_and_cwd_absolute(make_spec, workdir):
    spec = make_spec("claude")
    assert spec.cwd == workdir.resolve() and spec.cwd.is_absolute()


def test_spec_unknown_harness(stack, workdir, credentials):
    from phd.spec import Spec
    with pytest.raises(UnknownHarness, match="'gemini'"):
        Spec(harness="gemini", model="m", stack=stack, cwd=workdir, credentials=credentials["claude"])


def test_spec_missing_cwd(make_spec, tmp_path):
    with pytest.raises(CwdNotFound):
        make_spec("claude", cwd=tmp_path / "nope")


def test_spec_bad_modes(make_spec):
    with pytest.raises(InvalidSpec, match="project_config"):
        make_spec("claude", project_config="some")
    with pytest.raises(InvalidSpec, match="autonomy"):
        make_spec("claude", autonomy="yolo")
    with pytest.raises(InvalidSpec, match="timeout_s"):
        make_spec("claude", timeout_s=0)


def test_full_autonomy_needs_unsafe(make_spec):
    with pytest.raises(UnsafeAutonomy):
        make_spec("claude", autonomy="full")
    assert make_spec("claude", autonomy="full", unsafe=True).unsafe


def test_credentials_must_match_harness(make_spec, credentials):
    with pytest.raises(MissingCredentials, match="'codex'"):
        make_spec("claude", credentials=credentials["codex"])


def test_missing_stack_secret(make_spec):
    with pytest.raises(MissingSecret, match="ECHO_PREFIX"):
        make_spec("claude", secrets={})


def test_stack_compatibility_checked(make_spec, stack):
    import dataclasses
    claude_only = dataclasses.replace(stack, harnesses=("claude",))
    with pytest.raises(StackIncompatible):
        make_spec("codex", stack=claude_only)


def test_inputs_validation(tmp_path):
    with pytest.raises(EmptyTask):
        Inputs(msg="   ")
    with pytest.raises(InputNotFound, match="brief"):
        Inputs(brief=tmp_path / "missing.md")
    i = Inputs(brief=str(FIXTURES / "inputs" / "brief.md"))
    assert i.brief.is_absolute()


def test_credentials_repr_hides_secret():
    assert "s3cret" not in repr(Credentials.claude_oauth_token("s3cret"))


def test_claude_host_login_reads_access_token_only(tmp_path):
    f = tmp_path / ".credentials.json"
    f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "at-1", "refreshToken": "rt-SECRET"}}))
    c = Credentials.claude_host_login(f)
    assert c.kind == "claude_host_login" and c.claude_access_token() == "at-1"
    with pytest.raises(MissingCredentials):
        Credentials.claude_host_login(tmp_path / "absent.json")


def test_opencode_auth_content_filters_providers(credentials):
    assert json.loads(credentials["opencode"].opencode_auth_content()) == {"opencode": {"type": "api", "key": "oc-test"}}
