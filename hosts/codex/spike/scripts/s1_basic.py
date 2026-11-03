"""S1: start app-server with OpenCode Go provider, initialize, thread/start, one turn, streaming."""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s1")
print("initialize:", json.dumps(a.init())[:300])
t = a.request("thread/start", {"cwd": f"{ROOT}/work"})
th = t["thread"]["id"]; print("thread:", th, "model:", t.get("model"), "provider:", t.get("modelProvider"))
tid, done, notes = a.run_turn(th, "Reply with exactly: PONG-S1")
print("turn status:", done["params"]["turn"].get("status"), "error:", done["params"]["turn"].get("error"))
print("events:", methods(notes))
print("final:", final_text(notes))
a.close()
