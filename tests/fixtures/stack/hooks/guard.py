"""Neutral pre_tool hook: logs every shell call to $PHD_RUN_DIR/hook.log, blocks 'rm -rf'."""
import json
import os
import sys

evt = json.load(sys.stdin)
with open(os.path.join(os.environ.get("PHD_RUN_DIR", "."), "hook.log"), "a", encoding="utf-8") as f:
    f.write(json.dumps(evt) + "\n")
if "rm -rf" in json.dumps(evt.get("input", {})):
    print("blocked by guard.py: rm -rf is not allowed", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
