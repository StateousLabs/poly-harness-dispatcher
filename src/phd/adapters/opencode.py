"""OpenCode adapter (CoreDesign §6, verified §14.1)."""

from pathlib import Path

from .._fs import write_json
from ..errors import MissingCredentials, UnsupportedFeature
from ..hooks import check_supported, opencode_plugin, write_runtime
from ..parsers import OpencodeParser
from ..spec import Spec
from .base import MaterializeContext


class OpencodeAdapter:
    name = "opencode"

    def validate(self, spec: Spec) -> list[str]:
        check_supported("opencode", spec.stack.hooks)
        if spec.output_schema is not None:
            raise UnsupportedFeature("opencode: output_schema is not supported yet")
        return []

    def materialize(self, ctx: MaterializeContext) -> tuple[list[str], dict[str, str]]:
        spec, home = ctx.spec, ctx.home
        cfg_dir = home / "config"
        ctx.copy_skills(cfg_dir / "skills")
        (home / "AGENTS.md").write_text(ctx.prompt.system, encoding="utf-8")

        mcp = {}
        for name, server in ctx.mcp_servers().items():
            if server.get("type", "stdio") == "stdio":
                mcp[name] = {"type": "local", "command": [server["command"], *server.get("args", [])],
                             "environment": server.get("env", {}), "enabled": True}
            else:
                mcp[name] = {"type": "remote", "url": server["url"], "headers": server.get("headers", {}), "enabled": True}
        permission = ({"*": "allow"} if spec.autonomy == "full"
                      else {"edit": "allow", "bash": {"*": "deny"}, "external_directory": "deny"})
        config = {"$schema": "https://opencode.ai/config.json", "instructions": [(home / "AGENTS.md").as_posix()],
                  "mcp": mcp, "permission": permission, "autoupdate": False}

        hooks = ctx.hooks()
        if hooks:
            shim = write_runtime(home, run_id=ctx.run_id, run_dir=ctx.run_dir, hooks=hooks)
            (cfg_dir / "plugin").mkdir(parents=True, exist_ok=True)
            (cfg_dir / "plugin" / "phd-hooks.js").write_text(opencode_plugin(hooks, shim), encoding="utf-8")
        write_json(cfg_dir / "opencode.json", ctx.merge_harness_config(config))

        env = {"OPENCODE_CONFIG": str(cfg_dir / "opencode.json"), "OPENCODE_CONFIG_DIR": str(cfg_dir),
               "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "XDG_DATA_HOME": str(home / "data"),
               "XDG_CONFIG_HOME": str(home / "xdg-config"), "XDG_STATE_HOME": str(home / "state"),
               **self.credential_env(spec, home)}
        if spec.project_config != "full":
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        argv = ["opencode", "run", "--format", "json", "-m", spec.model, "--dir", str(spec.cwd)]
        if spec.autonomy == "full":
            argv.append("--auto")
        return argv, env

    def credential_env(self, spec: Spec, home: Path) -> dict[str, str]:
        c = spec.credentials
        if c.kind != "opencode_auth_file":
            raise MissingCredentials(f"opencode cannot use credentials of kind {c.kind!r}")
        return {"OPENCODE_AUTH_CONTENT": c.opencode_auth_content()}

    def resume_argv(self, base_argv: list[str], session_id: str, spec: Spec) -> list[str]:
        return [*base_argv, "--session", session_id]

    def parser(self) -> OpencodeParser:
        return OpencodeParser()
