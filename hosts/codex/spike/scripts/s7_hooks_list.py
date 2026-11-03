import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s7list", env_extra={"CODEX_HOME": f"{ROOT}/home_hooks"}); a.init()
r = a.request("hooks/list", {"cwds": [f"{ROOT}/work"]}, raise_err=False)
print(json.dumps(r, indent=1)[:3000]); a.close()
