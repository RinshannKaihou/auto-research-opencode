"""S11c: what does a turn look like when the x-opencode-session header is missing (expected provider 400 MissingSessionID)?"""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"; n0 = len(open(P).readlines())
a = AppServer("s11c", env_extra={"CODEX_HOME": f"{ROOT}/home_nohdr"}); a.init()  # home_nohdr = home/config.toml minus http_headers
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
_, d, n = a.run_turn(th, "Reply OK", timeout=180)
print("turn:", d["params"]["turn"]["status"], json.dumps(d["params"]["turn"].get("error"))[:300])
print("error notes:", [json.dumps(x["params"])[:200] for x in n if x.get("method") == "error"][:2])
print("proxy:", [(r["status"], r["session"], (r.get("err") or "")[:120]) for r in map(json.loads, open(P).readlines()[n0:])])
a.close()
