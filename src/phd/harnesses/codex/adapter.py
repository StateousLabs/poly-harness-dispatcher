"""Codex adapter (CoreDesign §6, verified §14.1–14.2)."""

from pathlib import Path

from ..._util import toml as _toml
from ..._util.fs import write_json, write_private
from ...errors import MissingCredentials
from ...materialize import MaterializeContext, check_supported, claude_style_block, write_runtime
from ...spec import Spec
from .parser import CodexParser
from .schema import restore, to_strict

RESUME_FLAGS = ("--json", "--strict-config", "--skip-git-repo-check", "--ignore-rules",
                "--dangerously-bypass-hook-trust")


def user_home() -> Path:
    """Codex resolves the user home through the OS, not $HOME, so it cannot be redirected."""
    return Path.home()


def foreign_skills(cwd: Path, project_config: str, home: Path) -> list[Path]:
    """SKILL.md files codex would discover outside the run home: the user's ~/.agents/skills and,
    unless project_config="full", repo-level .agents/skills from cwd up to the filesystem root."""
    roots = [home / ".agents" / "skills"]
    if project_config != "full":
        roots += [d / ".agents" / "skills" for d in (cwd, *cwd.parents)]
    found: list[Path] = []
    for root in dict.fromkeys(roots):
        if root.is_dir():
            found += sorted(root.glob("*/SKILL.md"))
    return found


class CodexAdapter:
    name = "codex"
    cumulative_cost = False  # reported cost is this invocation's own

    def validate(self, spec: Spec) -> list[str]:
        check_supported("codex", spec.stack.hooks)
        if spec.output_schema is not None:
            to_strict(spec.output_schema)  # raises UnsupportedFeature for a keyword codex cannot express
        warnings = []
        if spec.budget_usd is not None:
            warnings.append("codex reports no cost, so budget_usd cannot be enforced for this run")
        if any(s.get("type") == "http" for s in spec.stack.mcp_servers.values()):
            warnings.append("codex http MCP translation (url + http_headers) is unverified")
        return warnings

    def materialize(self, ctx: MaterializeContext) -> tuple[list[str], dict[str, str]]:
        spec, home = ctx.spec, ctx.home
        ctx.copy_skills(home / "skills")
        (home / "AGENTS.md").write_text(ctx.prompt.system, encoding="utf-8")

        config: dict = {"model": spec.model, "approval_policy": "never",
                        "sandbox_mode": "danger-full-access" if spec.autonomy == "full" else "workspace-write"}
        if spec.project_config != "full":
            config["project_doc_max_bytes"] = 0
        servers = {}
        for name, server in ctx.mcp_servers().items():
            if server.get("type", "stdio") == "stdio":
                entry = {"command": server["command"], "args": server.get("args", [])}
                if server.get("env"):
                    entry["env"] = server["env"]
            else:
                entry = {"url": server["url"]}
                if server.get("headers"):
                    entry["http_headers"] = server["headers"]
            entry["default_tools_approval_mode"] = "approve"  # else exec cancels MCP calls
            servers[name] = entry
        if servers:
            config["mcp_servers"] = servers
        foreign = foreign_skills(spec.cwd, spec.project_config, user_home())
        skills: dict = {}
        if foreign:
            skills["config"] = [{"path": p.as_posix(), "enabled": False} for p in foreign]
        if not spec.harness_builtins:
            skills["bundled"] = {"enabled": False}  # the `.system` skills codex installs into CODEX_HOME on start
        if skills:
            config["skills"] = skills
        (home / "config.toml").write_text(_toml.dumps(ctx.merge_harness_config(config)), encoding="utf-8")

        hooks = ctx.hooks()
        if hooks:
            shim = write_runtime(home, run_id=ctx.run_id, run_dir=ctx.run_dir, hooks=hooks)
            write_json(home / "hooks.json", {"hooks": claude_style_block("codex", hooks, shim)})

        argv = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
                "-C", str(spec.cwd), "-m", spec.model]
        if spec.project_config != "full":
            argv.append("--ignore-rules")
        if hooks:
            argv.append("--dangerously-bypass-hook-trust")  # PHD authored these hooks
        if spec.output_schema is not None:
            write_json(home / "output-schema.json", to_strict(spec.output_schema))
            argv += ["--output-schema", str(home / "output-schema.json")]
        argv.append("-")  # prompt on stdin
        return argv, {"CODEX_HOME": str(home), **self.credential_env(spec, home)}

    def owned_paths(self, home: Path) -> list[Path]:
        """Everything `materialize` writes under `home`; the rest (sessions/, *.sqlite, logs) is codex's."""
        return [home / "skills", home / "AGENTS.md", home / "config.toml", home / "hooks.json",
                home / "output-schema.json", home / "auth.json"]

    def credential_env(self, spec: Spec, home: Path) -> dict[str, str]:
        c = spec.credentials
        if c.kind != "codex_auth_file":
            raise MissingCredentials(f"codex cannot use credentials of kind {c.kind!r}")
        write_private(home / "auth.json", c.path.read_text(encoding="utf-8"))
        return {}

    def resume_argv(self, fresh_argv: list[str], session_id: str, spec: Spec) -> list[str]:
        kept = [a for a in fresh_argv if a in RESUME_FLAGS]
        if "--output-schema" in fresh_argv:
            i = fresh_argv.index("--output-schema")
            kept += fresh_argv[i:i + 2]
        # `exec resume` has no -C; the launcher always runs in spec.cwd
        return ["codex", "exec", "resume", session_id, *kept, "-m", spec.model, "-"]

    def restore_answer(self, spec: Spec, value):
        """The structured answer in the shape of `spec.output_schema` (unwrapped, optional nulls removed)."""
        return value if spec.output_schema is None else restore(spec.output_schema, value)

    def parser(self) -> CodexParser:
        return CodexParser()
