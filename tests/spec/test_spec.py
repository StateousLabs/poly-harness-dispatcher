import json

import pytest

from conftest import FIXTURES
from phd.spec import placeholders
from phd.errors import (CwdNotFound, EmptyTask, InputNotFound, InvalidSpec, MissingCredentials,
                        MissingSecret, StackIncompatible, UnknownHarness, UnsafeAutonomy)
from phd.spec import Credentials, Inputs
from phd.spec import Stack


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


def test_credentials_from_host_unknown_harness():
    with pytest.raises(UnknownHarness, match="'gemini'"):
        Credentials.from_host("gemini")


def test_claude_access_token_missing_field(tmp_path):
    f = tmp_path / ".credentials.json"
    f.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "rt"}}))
    c = Credentials.claude_host_login(f)
    with pytest.raises(MissingCredentials, match="accessToken"):
        c.claude_access_token()


def test_claude_access_token_malformed_json(tmp_path):
    f = tmp_path / ".credentials.json"
    f.write_text("not valid json {")
    c = Credentials.claude_host_login(f)
    with pytest.raises(MissingCredentials, match="failed to read or parse"):
        c.claude_access_token()


def test_claude_access_token_top_level_list(tmp_path):
    f = tmp_path / ".credentials.json"
    f.write_text(json.dumps([1, 2, 3]))
    c = Credentials.claude_host_login(f)
    with pytest.raises(MissingCredentials, match="top level must be"):
        c.claude_access_token()


def test_opencode_auth_content_missing_provider(tmp_path):
    f = tmp_path / "oc.json"
    f.write_text(json.dumps({"other": {"type": "api"}}))
    c = Credentials.opencode_auth_file(f, providers=["opencode"])
    with pytest.raises(MissingCredentials, match="opencode"):
        c.opencode_auth_content()


def test_opencode_auth_content_empty_providers(credentials):
    oc = credentials["opencode"]
    # With empty providers, all providers should be returned
    content = json.loads(Credentials.opencode_auth_file(oc.path, providers=[]).opencode_auth_content())
    assert "opencode" in content and "other" in content


def test_spec_budget_usd_zero(make_spec):
    with pytest.raises(InvalidSpec, match="budget_usd must be positive"):
        make_spec("claude", budget_usd=0)


def test_spec_model_whitespace(make_spec, stack, workdir, credentials):
    from phd.spec import Spec
    with pytest.raises(InvalidSpec, match="model must be"):
        Spec(harness="claude", model="  ", stack=stack, cwd=workdir, credentials=credentials["claude"])


def test_missing_hook_secret(tmp_path, stack, workdir, credentials):
    import shutil

    # Create a modified stack with a hook that uses a secret
    stack_copy = tmp_path / "stack"
    shutil.copytree(stack.root, stack_copy)

    # Modify hooks.json to use ${env:HOOK_TOKEN}
    hooks_file = stack_copy / "hooks" / "hooks.json"
    hooks_data = json.loads(hooks_file.read_text(encoding="utf-8"))
    hooks_data["hooks"][0]["command"] = "echo ${env:HOOK_TOKEN}"
    hooks_file.write_text(json.dumps(hooks_data))

    modified_stack = Stack.load(stack_copy)

    from phd.spec import Spec
    with pytest.raises(MissingSecret, match="HOOK_TOKEN"):
        Spec(harness="claude", model="haiku", stack=modified_stack, cwd=workdir,
             credentials=credentials["claude"], secrets={"ECHO_PREFIX": "phd-echo"})


def test_inputs_rules_not_found(tmp_path):
    with pytest.raises(InputNotFound, match="rules"):
        Inputs(msg="test", rules=tmp_path / "missing.md")


def test_placeholders_resolve_non_strings():
    value = {"num": 42, "bool": True, "list": [1, "${env:X}", None]}
    result = placeholders.resolve(value, stack_dir="/s", secrets={"X": "x"})
    assert result == {"num": 42, "bool": True, "list": [1, "x", None]}


def test_claude_host_login_hides_refresh_token(tmp_path):
    f = tmp_path / ".credentials.json"
    f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "at-1", "refreshToken": "rt-SECRET"}}))
    c = Credentials.claude_host_login(f)
    assert "rt-SECRET" not in repr(c)


def test_harness_builtins_defaults_off_and_must_be_bool(make_spec):
    assert make_spec("claude").harness_builtins is False
    assert make_spec("claude", harness_builtins=True).harness_builtins is True
    for bad in ("yes", 1, None):
        with pytest.raises(InvalidSpec, match="harness_builtins"):
            make_spec("claude", harness_builtins=bad)
