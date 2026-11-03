"""S3 part 2: fresh app-server process; thread/read (no resume) and thread/turns/list must show the same clientId."""
import json, sys
sys.path.insert(0, "/workspace/codex-spike/backfill/scripts")
from bf_client import *
OUT = open(f"{BF}/logs/bf_main.summary.txt", "a")
def out(*a):
    s = " ".join(str(x) for x in a); print(s); OUT.write(s + "\n"); OUT.flush()
st = json.load(open(f"{BF}/run/s3_state.json"))
a = BFServer("bf_s3_p2"); a.init()
out("\n## S3 after restart: new pid", a.p.pid)
th = a.request("thread/read", {"threadId": st["thread"], "includeTurns": True})["thread"]
um = [(t["id"], t["status"], it.get("clientId"), [c.get("text") for c in it.get("content", [])]) for t in th.get("turns", []) for it in t.get("items", []) if it.get("type") == "userMessage"]
out("thread/read userMessages:", um)
out("S3 MATCH (thread/read):", any(x[2] == st["cumid"] for x in um), "expected", st["cumid"])
r = a.request("thread/turns/list", {"threadId": st["thread"]}, raise_err=False)
tl = [it.get("clientId") for t in (r.get("data") or r.get("turns") or []) for it in t.get("items", []) if it.get("type") == "userMessage"] if isinstance(r, dict) else r
out("thread/turns/list clientIds:", tl if tl else json.dumps(r)[:300])
out("raw key hits in app-server stdout msgs (phase2):", RAW_KEY_HITS["stdout_msgs"])
a.close()
