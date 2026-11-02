"""S11: one static x-opencode-session per process across many threads: 6 threads x 2 turns concurrently (one tool call each)."""
import os, json, sys, threading, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"; n0 = len(open(P).readlines())
a = AppServer("s11"); a.init()
ths = [a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"] for _ in range(6)]
res = {}
def go(k, th):
    out = []
    _, d, n = a.run_turn(th, f"Remember number {k*111}. Call fixture step with i={k}. Reply with the token only.", timeout=300); out.append((d["params"]["turn"]["status"], final_text(n, th)))
    _, d, n = a.run_turn(th, "What number did I ask you to remember? Reply digits only.", timeout=300); out.append((d["params"]["turn"]["status"], final_text(n, th)))
    res[k] = out
t0 = time.time(); ts = [threading.Thread(target=go, args=(k, th)) for k, th in enumerate(ths, 1)]
[t.start() for t in ts]; [t.join() for t in ts]
print("wall:", round(time.time() - t0, 1))
for k in sorted(res): print(k, "expect", f"T{k*7%97:02d}", k * 111, "->", res[k])
rs = [json.loads(l) for l in open(P).readlines()[n0:]]
st = {}
for r in rs: st[r["status"]] = st.get(r["status"], 0) + 1
iv = [(r["t"], r["t"] + r["dt"]) for r in rs]
print("proxy requests:", len(rs), "status counts:", st, "sessions:", {r["session"] for r in rs},
      "max concurrency:", max(sum(1 for b in iv if b[0] <= a_[0] < b[1]) for a_ in iv))
a.close()
