"""Trust hooks by writing [hooks.state."<key>"].trusted_hash = currentHash (what /hooks does), from hooks/list."""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s7list", env_extra={"CODEX_HOME": f"{ROOT}/home_hooks"}); a.init()
hs = a.request("hooks/list", {"cwds": [f"{ROOT}/work"]})["data"][0]["hooks"]; a.close()
with open(f"{ROOT}/home_hooks/config.toml", "a") as f:
    for h in hs:
        f.write(f'\n[hooks.state."{h["key"]}"]\ntrusted_hash = "{h["currentHash"]}"\n')
a = AppServer("s7list", env_extra={"CODEX_HOME": f"{ROOT}/home_hooks"}); a.init()
print([(h["eventName"], h["trustStatus"]) for h in a.request("hooks/list", {"cwds": [f"{ROOT}/work"]})["data"][0]["hooks"]]); a.close()
