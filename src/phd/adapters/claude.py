"""Claude Code adapter (CoreDesign §6, verified §14.1)."""

import json
from pathlib import Path

from .._fs import write_json
from ..errors import MissingCredentials
from ..hooks import check_supported, claude_style_block, write_runtime
from ..parsers import ClaudeParser
from ..spec import Spec
from .base import MaterializeContext


class ClaudeAdapter:
    name = "claude"

    def validate(self, spec: Spec) -> list[str]:
        check_supported("claude", spec.stack.hooks)
        return []

    def materialize(self, ctx: MaterializeContext) -> tuple[list[str], dict[str, str]]:
        spec, home = ctx.spec, ctx.home
        ctx.copy_skills(home / "skills")
        (home / "system-prompt.md").write_text(ctx.prompt.system, encoding="utf-8")
        servers = ctx.mcp_servers()
        write_json(home / "mcp.json", {"mcpServers": servers})

        settings: dict = {"permissions": {"allow": ["Skill", *(f"mcp__{name}" for name in servers)]}}
        hooks = ctx.hooks()
        if hooks:
            shim = write_runtime(home, run_id=ctx.run_id, run_dir=ctx.run_dir, hooks=hooks)
            settings["hooks"] = claude_style_block("claude", hooks, shim)
        write_json(home / "settings.json", ctx.merge_harness_config(settings))

        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", spec.model,
                "--append-system-prompt-file", str(home / "system-prompt.md"),
                "--strict-mcp-config", "--mcp-config", str(home / "mcp.json"),
                "--setting-sources", "user,project,local" if spec.project_config == "full" else "user"]
        if spec.autonomy == "full":
            argv.append("--dangerously-skip-permissions")
        else:
            argv += ["--permission-mode", "acceptEdits"]
        if spec.budget_usd is not None:
            argv += ["--max-budget-usd", str(spec.budget_usd)]
        if spec.output_schema is not None:
            argv += ["--json-schema", json.dumps(spec.output_schema)]
        env = {"CLAUDE_CONFIG_DIR": str(home), "DISABLE_AUTOUPDATER": "1", **self.credential_env(spec, home)}
        return argv, env

    def credential_env(self, spec: Spec, home: Path) -> dict[str, str]:
        c = spec.credentials
        if c.kind == "claude_oauth_token":
            return {"CLAUDE_CODE_OAUTH_TOKEN": c.secret}
        if c.kind == "anthropic_api_key":
            return {"ANTHROPIC_API_KEY": c.secret}
        if c.kind == "claude_host_login":
            return {"CLAUDE_CODE_OAUTH_TOKEN": c.claude_access_token()}
        raise MissingCredentials(f"claude cannot use credentials of kind {c.kind!r}")

    def resume_argv(self, base_argv: list[str], session_id: str, spec: Spec) -> list[str]:
        return [*base_argv, "--resume", session_id]

    def parser(self) -> ClaudeParser:
        return ClaudeParser()
