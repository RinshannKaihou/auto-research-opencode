"""S9: are compaction/truncation events observable? manual thread/compact/start + auto-compaction with low limit."""
import os, json, sys, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"
a = AppServer("s9"); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
a.run_turn(th, "Remember codeword MANGO-5522. Reply OK.")
s = len(a.notes); n0 = len(open(P).readlines())
print("compact/start:", a.request("thread/compact/start", {"threadId": th}, raise_err=False))
try:
    a.wait_note(lambda n: n.get("method") in ("thread/compacted", "turn/completed") and n["params"].get("threadId") == th, 180, s)
except Exception as e: print("wait:", e)
time.sleep(2)
ev = a.notes[s:]
print("manual compaction events:", methods(ev))
print("compaction items:", [n["params"]["item"]["type"] for n in ev if n.get("method") == "item/completed"])
print("proxy reqs during compaction:", [(r["status"], r["n_input"], r["n_tools"], r["out_types"]) for r in map(json.loads, open(P).readlines()[n0:])])
_, d, n2 = a.run_turn(th, "What codeword did I give you? Reply CODE=<..>")
print("after manual compaction:", final_text(n2))
# auto compaction: new thread with low limit
th2 = a.request("thread/start", {"cwd": f"{ROOT}/work", "config": {"model_auto_compact_token_limit": 7000}})["thread"]["id"]
s = len(a.notes)
a.run_turn(th2, "Remember codeword PAPAYA-7788. Reply OK.")
for i in range(3):
    a.run_turn(th2, f"Call the fixture tool lookup with key {'alpha' if i % 2 == 0 else 'beta'} and reply with its label only.")
_, d, n3 = a.run_turn(th2, "What codeword did I give you at the start? Reply CODE=<..>")
ev = a.notes[s:]
print("auto: events:", {k: v for k, v in methods(ev).items() if "ompact" in k or k in ("warning", "error")})
print("auto: compaction items:", [n["params"]["item"]["type"] for n in ev if n.get("method") == "item/completed" and "ompact" in n["params"]["item"]["type"]])
print("auto: warnings:", [n["params"].get("message")[:150] for n in ev if n.get("method") == "warning"][:3])
print("auto: final:", final_text(n3), "| context usage last:", [n["params"]["tokenUsage"]["last"]["inputTokens"] for n in ev if n.get("method") == "thread/tokenUsage/updated"])
a.close()
