"""S4: per-thread / per-turn differences: developer instructions, model settings (effort), MCP tool allowlists."""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"
def proxy_since(n): return [json.loads(l) for l in open(P).readlines()[n:]]
a = AppServer("s4"); a.init()
A = a.request("thread/start", {"cwd": f"{ROOT}/work", "developerInstructions": "Always end every reply with the tag [ALPHA-THREAD].",
     "config": {"mcp_servers.fixture.enabled_tools": ["lookup"]}})
B = a.request("thread/start", {"cwd": f"{ROOT}/work", "developerInstructions": "Always end every reply with the tag [BETA-THREAD].",
     "config": {"mcp_servers": {"fixture": {"command": "python3", "args": [f"{ROOT}/scripts/mcp_fixture.py"], "enabled_tools": ["step"]}}}})
C = a.request("thread/start", {"cwd": f"{ROOT}/work", "config": {"mcp_servers.fixture.enabled": False}})
ids = {x["thread"]["id"]: name for x, name in ((A, "A"), (B, "B"), (C, "C"))}
q = "List the exact names of every fixture MCP tool you have access to (or say NONE), in one short line."
for th, name in ids.items():
    n0 = len(open(P).readlines())
    _, d, notes = a.run_turn(th, q)
    reqs = proxy_since(n0)
    print(name, "final:", final_text(notes), "| proxy tools:", reqs[-1]["tool_names"] if reqs else None)
# per-turn model settings: effort override
Ath = A["thread"]["id"]
for eff in ("low", "high"):
    n0 = len(open(P).readlines())
    _, d, notes = a.run_turn(Ath, "Reply with one word: done", effort=eff)
    reqs = proxy_since(n0)
    print(f"effort={eff}: proxy reasoning field:", [r["reasoning"] for r in reqs], "reasoning_tokens:",
          [(r.get("usage") or {}).get("output_tokens_details") for r in reqs], "final:", final_text(notes))
# per-turn model override to a bogus model (does the turn honor model param?)
n0 = len(open(P).readlines())
_, d, notes = a.run_turn(Ath, "Reply with one word: ok", model="deepseek-v4-flash-does-not-exist")
reqs = proxy_since(n0)
print("model override: proxy models:", [r["model"] for r in reqs], [r["status"] for r in reqs], "turn:", d["params"]["turn"]["status"],
      json.dumps(d["params"]["turn"].get("error"))[:300])
print("thread/read A settings:", json.dumps({k: v for k, v in a.request("thread/read", {"threadId": Ath})["thread"].items() if k != "turns"})[:600])
a.close()
