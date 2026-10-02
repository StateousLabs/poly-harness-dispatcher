from phd.materialize.prompt import assemble
from phd.spec import Inputs


def test_system_has_identity_then_rules_and_user_has_brief_then_msg(stack, inputs, workdir):
    p = assemble(stack.identity, inputs, workdir, "ignore")
    assert p.system.index("HELPER-IDENTITY-OK") < p.system.index("## Project rules (binding)")
    assert "RULES-OK" in p.system
    assert p.user.startswith("# Task brief\n\nSelf-check task")
    assert p.user.index("# Task brief") < p.user.index("# Message\n\nÜnïcode check — café")


def test_ignore_mode_never_reads_project_files(stack, inputs, workdir):
    p = assemble(stack.identity, inputs, workdir, "ignore")
    assert "PINEAPPLE" not in p.system and "MANGO" not in p.system


def test_instructions_only_appends_project_files(stack, inputs, workdir):
    p = assemble(stack.identity, inputs, workdir, "instructions_only")
    assert "## Project context (AGENTS.md)" in p.system and "MANGO" in p.system
    assert "## Project context (CLAUDE.md)" in p.system and "PINEAPPLE" in p.system


def test_full_mode_leaves_project_files_to_the_harness(stack, inputs, workdir):
    assert "PINEAPPLE" not in assemble(stack.identity, inputs, workdir, "full").system


def test_msg_only(stack, workdir):
    p = assemble(stack.identity, Inputs(msg="hi"), workdir, "ignore")
    assert p.user == "# Message\n\nhi\n"
    assert "## Project rules" not in p.system

