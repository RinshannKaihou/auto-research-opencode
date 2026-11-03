"""S2 web-search follow-up: one more turn with an explicit instruction to call the hosted `web_search` tool."""
import json, sys
sys.path.insert(0, "/workspace/codex-spike/backfill/scripts")
from bf_client import *
OUT = open(f"{BF}/logs/bf_main.summary.txt", "a")
P = f"{BF}/logs/proxy.jsonl"
n0 = len(open(P).readlines())
a = BFServer("bf_web_retry"); a.init()
W = a.request("thread/start", {"cwd": f"{BF}/work/s2", "config": {"web_search": "live", "features.multi_agent": False, "features.shell_tool": False, "features.unified_exec": False}})["thread"]["id"]
tid, done, notes = a.run_turn(W, "You have a tool of type `web_search` in your tool list. Call it now with the query `opencode zen go models`. After it returns, reply with the title of the first result. Only if the call is impossible reply exactly NO_WEB_TOOL.", timeout=180)
reqs = [json.loads(l) for l in open(P).readlines()[n0:]]
s = (f"\n## S2-W2 web_search live (retry, explicit) thread={W} status={done['params']['turn']['status']} model_calls={len(reqs)}\n"
     f"  tools sent: {[r.get('tool_names') for r in reqs]}\n  provider out_types: {[r.get('out_types') for r in reqs]}\n"
     f"  item types: {[n['params']['item']['type'] for n in notes if n.get('method')=='item/completed']}\n  final: {json.dumps(final_text(notes))[:300]}\n"
     f"  raw key hits: {RAW_KEY_HITS['stdout_msgs']}")
print(s); OUT.write(s + "\n"); a.close()
