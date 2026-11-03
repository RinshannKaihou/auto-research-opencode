"""S5: interrupt a running turn (during a slow MCP tool and during streaming); resume a thread after app-server restart."""
import os, json, sys, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s5"); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
# 5a: interrupt during slow tool
start = len(a.notes)
tid = a.request("turn/start", {"threadId": th, "input": [{"type": "text", "text": "Call the fixture tool slow with seconds=40, then reply DONE."}]})["turn"]["id"]
a.wait_note(lambda n: n.get("method") == "item/started" and n["params"]["item"]["type"] == "mcpToolCall", 120, start)
t0 = time.time(); print("interrupt resp:", a.request("turn/interrupt", {"threadId": th, "turnId": tid}))
d = a.turn_completed(th, tid, 60)
print("5a turn status after interrupt:", d["params"]["turn"]["status"], "latency s:", round(time.time() - t0, 2))
print("5a events:", methods(a.notes[start:]))
# 5b: interrupt during long text streaming
start = len(a.notes)
tid2 = a.request("turn/start", {"threadId": th, "input": [{"type": "text", "text": "Write the numbers from 1 to 400 in words, one per line. No tools."}]})["turn"]["id"]
a.wait_note(lambda n: n.get("method") == "item/agentMessage/delta" and n["params"].get("turnId") == tid2, 120, start)
time.sleep(1.0); t0 = time.time(); a.request("turn/interrupt", {"threadId": th, "turnId": tid2})
d2 = a.turn_completed(th, tid2, 60)
print("5b status:", d2["params"]["turn"]["status"], "latency s:", round(time.time() - t0, 2), "deltas:", methods(a.notes[start:]).get("item/agentMessage/delta"))
# follow-up turn on same thread still works
_, d3, n3 = a.run_turn(th, "Remember codeword KIWI-9031. Reply only OK.")
print("post-interrupt turn:", d3["params"]["turn"]["status"], final_text(n3))
a.close(); time.sleep(1)
# 5c: restart and resume
b = AppServer("s5b"); b.init()
r = b.request("thread/resume", {"threadId": th, "cwd": f"{ROOT}/work"})
print("resume: turns in history:", len(r["thread"].get("turns") or []), "statuses:", [t["status"] for t in r["thread"].get("turns") or []])
_, d4, n4 = b.run_turn(th, "What codeword did I give you? Reply CODE=<..>")
print("after restart:", d4["params"]["turn"]["status"], final_text(n4))
print("thread/list count:", len(b.request("thread/list", {"limit": 50})["data"]))
b.close()
