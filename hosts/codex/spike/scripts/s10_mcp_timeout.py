"""S10: MCP tool timeout behavior with a slow tool: default (60s) with slow(75); per-thread tool_timeout_sec=5 with slow(10).
Also checks whether the MCP server process env contains OPENCODE_* (count only)."""
import os, json, sys, time, threading, subprocess; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s10"); a.init()
A = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
B = a.request("thread/start", {"cwd": f"{ROOT}/work", "config": {"mcp_servers.fixture.tool_timeout_sec": 5}})["thread"]["id"]
res = {}
def go(th, secs, name):
    t0 = time.time()
    tid, d, notes = a.run_turn(th, f"Call the fixture tool slow with seconds={secs}. Do not retry it. Then reply RESULT=<what happened>.", timeout=400)
    mc = [n["params"]["item"] for n in notes if n.get("method") == "item/completed" and n["params"].get("threadId") == th and n["params"]["item"]["type"] == "mcpToolCall"]
    res[name] = dict(status=d["params"]["turn"]["status"], wall=round(time.time() - t0, 1),
                     mcp=[(m.get("status"), json.dumps(m.get("error"))[:200], m.get("durationMs")) for m in mc], final=final_text(notes, th))
ts = [threading.Thread(target=go, args=(A, 75, "default60_slow75")), threading.Thread(target=go, args=(B, 10, "timeout5_slow10"))]
[t.start() for t in ts]; time.sleep(8)
pids = subprocess.run(["pgrep", "-f", "mcp_fixture.py"], capture_output=True, text=True).stdout.split()
cnt = []
for p in pids:
    try: cnt.append(sum(1 for kv in open(f"/proc/{p}/environ", "rb").read().split(b"\0") if kv.startswith(b"OPENCODE")))
    except Exception as e: cnt.append(repr(e)[:40])
print("MCP server processes:", len(pids), "OPENCODE* vars in each env:", cnt)
[t.join() for t in ts]
for k, v in res.items(): print(k, json.dumps(v))
a.close()
