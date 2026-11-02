"""S11b: can the x-opencode-session header be overridden per thread via thread/start config?"""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
P = f"{ROOT}/logs/proxy.jsonl"; n0 = len(open(P).readlines())
a = AppServer("s11b"); a.init()
for cfg in ({"model_providers.opencode_go.http_headers": {"x-opencode-session": "per-thread-AAA"}},
            {"model_providers": {"opencode_go": {"name": "OpenCode Go", "base_url": "http://127.0.0.1:18080/v1", "env_key": "OPENCODE_API_KEY",
             "wire_api": "responses", "http_headers": {"x-opencode-session": "per-thread-BBB"}}}}):
    r = a.request("thread/start", {"cwd": f"{ROOT}/work", "config": cfg}, raise_err=False)
    if "thread" not in r: print("thread/start error:", json.dumps(r)[:300]); continue
    _, d, n = a.run_turn(r["thread"]["id"], "Reply OK")
    print("turn:", d["params"]["turn"]["status"], "session header seen:", json.loads(open(P).readlines()[-1])["session"])
a.close()
