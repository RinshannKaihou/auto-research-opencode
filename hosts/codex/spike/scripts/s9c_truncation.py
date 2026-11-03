"""S9c: is tool-output truncation observable? Shell command printing ~400KB; compare client-visible output vs what was sent to the model."""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"; n0 = len(open(P).readlines())
a = AppServer("s9c"); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
_, d, n = a.run_turn(th, "Run exactly this shell command once: seq 1 80000   Then reply with the LAST number you can see in the output, as LAST=<n>.")
cmd = [x["params"]["item"] for x in n if x.get("method") == "item/completed" and x["params"]["item"]["type"] == "commandExecution"]
print("client aggregatedOutput bytes:", [len(c.get("aggregatedOutput") or "") for c in cmd], "| final:", final_text(n))
print("events:", {k: v for k, v in methods(n).items() if "trunc" in k.lower() or k == "warning"})
rs = [json.loads(l) for l in open(P).readlines()[n0:]]
print("proxy input tokens per call:", [(r.get("usage") or {}).get("input_tokens") for r in rs])
a.close()
