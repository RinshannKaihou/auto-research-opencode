"""S1b: same as S1 but bypassing the logging proxy (direct base_url), for both documented base URLs."""
import os, json, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
for url in ("https://opencode.ai/zen/go/v1", "https://opencode.ai/inference/go/openai/v1"):
    a = AppServer("s1b", extra_args=["-c", f'model_providers.opencode_go.base_url="{url}"']); a.init()
    th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
    _, d, n = a.run_turn(th, "Call fixture step with i=5, then reply with the token only.")
    print(url, "->", d["params"]["turn"]["status"], final_text(n), json.dumps(d["params"]["turn"].get("error"))[:200]); a.close()
