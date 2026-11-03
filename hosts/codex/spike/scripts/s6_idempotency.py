"""S6: clientUserMessageId / idempotency for retried turn/start."""
import os, json, sys, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"
a = AppServer("s6"); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
inp = [{"type": "text", "text": "Reply with exactly: ACK"}]
# (1) sequential duplicate: same clientUserMessageId after first turn completed
n0 = len(open(P).readlines())
r1 = a.request("turn/start", {"threadId": th, "input": inp, "clientUserMessageId": "cumid-001"}); a.turn_completed(th, r1["turn"]["id"])
r2 = a.request("turn/start", {"threadId": th, "input": inp, "clientUserMessageId": "cumid-001"}, raise_err=False)
print("seq dup: r1 turn", r1["turn"]["id"][-6:], "| r2:", json.dumps(r2)[:300])
if "turn" in r2: a.turn_completed(th, r2["turn"]["id"])
# (2) concurrent duplicate: fire two turn/start with same id back-to-back without waiting
s = len(a.notes)
i1 = next(a.ids); a.send({"jsonrpc": "2.0", "id": i1, "method": "turn/start", "params": {"threadId": th, "input": inp, "clientUserMessageId": "cumid-002"}})
i2 = next(a.ids); a.send({"jsonrpc": "2.0", "id": i2, "method": "turn/start", "params": {"threadId": th, "input": inp, "clientUserMessageId": "cumid-002"}})
time.sleep(8)
print("conc dup resp1:", json.dumps(a.pending.get(i1))[:300]); print("conc dup resp2:", json.dumps(a.pending.get(i2))[:300])
time.sleep(5)
print("conc events:", methods(a.notes[s:]))
th_read = a.request("thread/read", {"threadId": th, "includeTurns": True})["thread"]
turns = th_read.get("turns") or []
print("turns on thread:", len(turns))
for t in turns:
    um = [it for it in t.get("items", []) if it.get("type") == "userMessage"]
    # Echoed field is item.clientId (REPORT.md S6), not the request's clientUserMessageId.
    print("  turn", t["id"][-6:], t["status"], "userMessages:", [(u.get("clientId"), u.get("id")) for u in um])
print("model requests made:", len(open(P).readlines()) - n0)
a.close()
