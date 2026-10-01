"""Names shared across PHD modules."""

HARNESSES: tuple[str, ...] = ("claude", "codex", "opencode")
PROJECT_CONFIG_MODES: tuple[str, ...] = ("ignore", "instructions_only", "full")
AUTONOMY_MODES: tuple[str, ...] = ("restricted", "full")
NEUTRAL_EVENTS: tuple[str, ...] = ("session_start", "pre_tool", "post_tool", "stop")
NEUTRAL_TOOLS: tuple[str, ...] = ("shell", "read", "write", "edit", "web_fetch", "web_search")
REDACTED = "<redacted>"
