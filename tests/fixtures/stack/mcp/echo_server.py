"""Minimal stdio MCP server (stdlib only) exposing one tool: echo(text)."""
import json
import os
import sys

PREFIX = os.environ.get("ECHO_PREFIX", "echo")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method = req.get("id"), req.get("method")
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req["params"].get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
            "name": "echo", "description": "Echo text back with a prefix.",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}})
    elif method == "tools/call":
        text = req["params"]["arguments"].get("text", "")
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"{PREFIX}: {text}"}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
