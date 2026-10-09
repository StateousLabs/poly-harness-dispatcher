"""OpenCode adapter (CoreDesign §6, verified §14.1)."""

from pathlib import Path

from ..._util.fs import write_json
from ...errors import MissingCredentials
from ...materialize import MaterializeContext, check_supported, opencode_plugin, write_runtime
from ...spec import Spec
from ...spec.output_schema import answer_instruction
from . import deps
from .models import shared_cache_dir
from .parser import OpencodeParser

# opencode 1.18.18 hardcodes one built-in skill with no disable switch; denying it through permissions
# removes it from the model's skill list (Skill.available filters on the agent's permission).
BUILTIN_SKILLS = ("customize-opencode",)


class OpencodeAdapter:
    name = "opencode"
    cumulative_cost = False  # reported cost is this invocation's own

    def validate(self, spec: Spec) -> list[str]:
        check_supported("opencode", spec.stack.hooks)
        return []

    def materialize(self, ctx: MaterializeContext) -> tuple[list[str], dict[str, str]]:
        spec, home = ctx.spec, ctx.home
        cfg_dir = home / "config"
        ctx.copy_skills(cfg_dir / "skills")
        system = ctx.prompt.system + (answer_instruction(spec.output_schema) if spec.output_schema is not None else "")
        (home / "AGENTS.md").write_text(system, encoding="utf-8")

        mcp = {}
        for name, server in ctx.mcp_servers().items():
            if server.get("type", "stdio") == "stdio":
                mcp[name] = {"type": "local", "command": [server["command"], *server.get("args", [])],
                             "environment": server.get("env", {}), "enabled": True}
            else:
                mcp[name] = {"type": "remote", "url": server["url"], "headers": server.get("headers", {}), "enabled": True}
        permission = ({"*": "allow"} if spec.autonomy == "full"
                      else {"edit": "allow", "bash": {"*": "deny"}, "external_directory": "deny"})
        if not spec.harness_builtins:
            permission["skill"] = {name: "deny" for name in BUILTIN_SKILLS}
        config = {"$schema": "https://opencode.ai/config.json", "instructions": [(home / "AGENTS.md").as_posix()],
                  "mcp": mcp, "permission": permission, "autoupdate": False}

        hooks = ctx.hooks()
        if hooks:
            shim = write_runtime(home, run_id=ctx.run_id, run_dir=ctx.run_dir, hooks=hooks)
            (cfg_dir / "plugin").mkdir(parents=True, exist_ok=True)
            (cfg_dir / "plugin" / "phd-hooks.js").write_text(opencode_plugin(hooks, shim), encoding="utf-8")
        write_json(cfg_dir / "opencode.json", ctx.merge_harness_config(config))
        deps.seed_home(home)  # else opencode npm-installs into both config folders and waits (deps.py)
        # shared by all runs of this opencode install; never the host's ~/.cache/opencode, never per run
        xdg_cache = shared_cache_dir(ctx.executable)
        xdg_cache.mkdir(parents=True, exist_ok=True)

        env = {"OPENCODE_CONFIG": str(cfg_dir / "opencode.json"), "OPENCODE_CONFIG_DIR": str(cfg_dir),
               "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "XDG_DATA_HOME": str(home / "data"),
               "XDG_CONFIG_HOME": str(home / "xdg-config"), "XDG_STATE_HOME": str(home / "state"),
               "XDG_CACHE_HOME": str(xdg_cache), **deps.npm_env(),
               **self.credential_env(spec, home)}
        if spec.project_config != "full":
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        argv = ["opencode", "run", "--format", "json", "-m", spec.model, "--dir", str(spec.cwd)]
        if spec.autonomy == "full":
            argv.append("--auto")
        return argv, env

    def owned_paths(self, home: Path) -> list[Path]:
        """Everything `materialize` writes under `home`; the rest (data/, state/, and whatever opencode installs
        into config/) is opencode's. The dependency seed (`deps.seed_home`) is not owned: it is written only when
        absent and, like opencode's own install, survives a resumed turn."""
        cfg_dir = home / "config"
        return [cfg_dir / "skills", cfg_dir / "plugin" / "phd-hooks.js", cfg_dir / "opencode.json", home / "AGENTS.md"]

    def credential_env(self, spec: Spec, home: Path) -> dict[str, str]:
        c = spec.credentials
        if c.kind != "opencode_auth_file":
            raise MissingCredentials(f"opencode cannot use credentials of kind {c.kind!r}")
        return {"OPENCODE_AUTH_CONTENT": c.opencode_auth_content()}

    def resume_argv(self, fresh_argv: list[str], session_id: str, spec: Spec) -> list[str]:
        return [*fresh_argv, "--session", session_id]

    def restore_answer(self, spec: Spec, value):
        return value  # the schema is only an instruction in AGENTS.md; the answer is already in the caller's shape

    def parser(self) -> OpencodeParser:
        return OpencodeParser()
