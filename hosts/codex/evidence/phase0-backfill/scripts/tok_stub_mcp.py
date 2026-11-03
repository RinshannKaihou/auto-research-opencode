#!/usr/bin/env python3
"""S2 MCP stub. Records whether RESEARCH_TOKEN is present (bool + sha256[:8] of the *test* token, never the value)
and whether any OPENCODE* var is present. Tool: whoami()."""
import json, sys, os, time, hashlib
LOG = os.environ.get("STUB_LOG", "/workspace/codex-spike/backfill/logs/tokstub.jsonl")
tok = os.environ.get("RESEARCH_TOKEN")
info = {"pid": os.getpid(), "ppid": os.getppid(), "token_present": tok is not None,
        "token_sha8": hashlib.sha256(tok.encode()).hexdigest()[:8] if tok else None,
        "opencode_vars": sum(k.startswith("OPENCODE") for k in os.environ)}
def lg(o):
    with open(LOG, "a") as f: f.write(json.dumps({"t": round(time.time(), 2), **o}) + "\n")
lg({"event": "start", **info})
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
TOOLS = [{"name": "whoami", "description": "Returns whether a research token is configured (presence + short hash only).",
          "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    try: m = json.loads(line)
    except Exception: continue
    mid, meth, p = m.get("id"), m.get("method"), m.get("params") or {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p.get("protocolVersion", "2025-06-18"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "tokstub", "version": "0.1"}}})
    elif meth == "tools/list": send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif meth == "tools/call":
        lg({"event": "call", **info})
        res = {"token_present": info["token_present"], "token_sha8": info["token_sha8"]}
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(res)}], "isError": False}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}} if meth == "ping" else {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nf"}})
