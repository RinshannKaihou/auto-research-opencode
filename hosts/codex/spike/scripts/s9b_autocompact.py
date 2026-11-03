"""S9b: auto-compaction with a very low per-thread model_auto_compact_token_limit (6200) so it must trigger."""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"
a = AppServer("s9b"); a.init()
th2 = a.request("thread/start", {"cwd": f"{ROOT}/work", "config": {"model_auto_compact_token_limit": 6200}})["thread"]["id"]
s = len(a.notes); n0 = len(open(P).readlines())
a.run_turn(th2, "Remember codeword PAPAYA-7788. Reply OK.")
for i in range(2):
    a.run_turn(th2, f"Call the fixture tool lookup with key {'alpha' if i % 2 == 0 else 'beta'} and reply with its label only.")
_, d, n3 = a.run_turn(th2, "What codeword did I give you at the start? Reply CODE=<..>")
ev = a.notes[s:]
print("events w/ compact:", {k: v for k, v in methods(ev).items() if "ompact" in k})
print("compaction items:", [(n["method"], n["params"]["item"]["type"]) for n in ev if n.get("method") in ("item/started", "item/completed") and "ompact" in n["params"]["item"]["type"]])
print("final:", final_text(n3))
print("input tokens per call:", [n["params"]["tokenUsage"]["last"]["inputTokens"] for n in ev if n.get("method") == "thread/tokenUsage/updated"])
print("proxy n_input per call:", [r["n_input"] for r in map(json.loads, open(P).readlines()[n0:])])
a.close()
