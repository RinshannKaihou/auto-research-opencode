#!/usr/bin/env python3
"""Tiny stdio MCP server (newline-delimited JSON-RPC, no deps).
Tools: lookup(key) -> fixed JSON; step(i) -> {"i":i,"next":i+1,"token":...};
slow(seconds) -> sleeps then returns. Logs calls to $MCP_LOG."""
import json, sys, time, os
LOG = os.environ.get("MCP_LOG", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "mcp_calls.jsonl"))
FIX = {"alpha": {"id": 1, "label": "ALPHA-7731", "tags": ["x", "y"]},
       "beta": {"id": 2, "label": "BETA-0042", "tags": []}}
TOOLS = [
 {"name": "lookup", "description": "Look up a fixture record by key (alpha or beta). Returns fixed JSON.",
  "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
 {"name": "step", "description": "Advance a counter. Call with i; returns next i and a check token.",
  "inputSchema": {"type": "object", "properties": {"i": {"type": "integer"}}, "required": ["i"]}},
 {"name": "slow", "description": "Sleeps for the given seconds then returns done.",
  "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
]
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def lg(o):
    with open(LOG, "a") as f: f.write(json.dumps({"t": round(time.time(), 2), "pid": os.getpid(), **o}) + "\n")
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: m = json.loads(line)
    except Exception: continue
    mid, meth, p = m.get("id"), m.get("method"), m.get("params") or {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p.get("protocolVersion", "2025-06-18"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "0.1"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif meth == "tools/call":
        name, a = p.get("name"), p.get("arguments") or {}
        lg({"tool": name, "args": a})
        if name == "lookup":
            res = FIX.get(a.get("key"), {"error": "unknown key"})
        elif name == "step":
            i = int(a.get("i", 0)); res = {"i": i, "next": i + 1, "token": f"T{i*7%97:02d}"}
        elif name == "slow":
            time.sleep(float(a.get("seconds", 1))); res = {"done": True, "slept": a.get("seconds")}
            lg({"tool": "slow", "finished": True})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}); continue
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(res)}],
              "structuredContent": res, "isError": False}})
    elif mid is not None:
        if meth == "ping": send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else: send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
