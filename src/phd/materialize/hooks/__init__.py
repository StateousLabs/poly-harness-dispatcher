"""Neutral hooks (CoreDesign §10) → harness-native hook config."""

from .opencode_plugin import opencode_plugin
from .runtime import write_runtime
from .translate import check_supported, claude_style_block, native_tool

__all__ = ["check_supported", "claude_style_block", "native_tool", "opencode_plugin", "write_runtime"]
