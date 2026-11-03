"""Trust the two S11 probe hooks (hooks/list -> write [hooks.state] trusted_hash), no model calls."""
import sys; sys.path.insert(0, "/workspace/codex-spike/backfill/scripts")
from bf_client import *
H = f"{BF}/home/config.toml"
a = BFServer("trust"); a.init()
hs = a.request("hooks/list", {"cwds": [f"{BF}/work"]})["data"][0]["hooks"]; a.close()
if "[hooks.state." not in open(H).read():
    with open(H, "a") as f:
        for h in hs: f.write(f'\n[hooks.state."{h["key"]}"]\ntrusted_hash = "{h["currentHash"]}"\n')
a = BFServer("trust"); a.init()
print([(h["eventName"], h["command"][-40:], h["trustStatus"]) for h in a.request("hooks/list", {"cwds": [f"{BF}/work"]})["data"][0]["hooks"]]); a.close()
