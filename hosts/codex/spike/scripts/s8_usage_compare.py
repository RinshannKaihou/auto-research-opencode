"""S8: compare app-server thread/tokenUsage/updated (per model call 'last' + cumulative 'total') with provider
usage captured by the logging proxy (response.completed.usage), for the S2 run.
logs/s2.start is the proxy.jsonl line offset written by s2_tools.py before that turn."""
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
start = int(open(f"{ROOT}/logs/s2.start").read())
prox = [json.loads(l) for l in open(f"{ROOT}/logs/proxy.jsonl").readlines()[start:start + 40]]
ids = json.load(open(f"{ROOT}/logs/s2_ids.json"))
tu = [json.loads(l)["params"]["tokenUsage"] for l in open(f"{ROOT}/logs/s2.jsonl") if '"thread/tokenUsage/updated"' in l and ids["thread"] in l]
prox = prox[:len(tu)]
mism = 0
for i, (p, t) in enumerate(zip(prox, tu)):
    u = p["usage"]; L = t["last"]
    ok = (u["input_tokens"] == L["inputTokens"] and u["output_tokens"] == L["outputTokens"]
          and u["input_tokens_details"]["cached_tokens"] == L["cachedInputTokens"])
    mism += not ok
print(f"model calls: proxy={len(prox)} tokenUsage events={len(tu)} per-call mismatches={mism}")
pi = sum(p["usage"]["input_tokens"] for p in prox); po = sum(p["usage"]["output_tokens"] for p in prox)
pc = sum(p["usage"]["input_tokens_details"]["cached_tokens"] for p in prox)
T = tu[-1]["total"]
print(f"provider sum: input={pi} cached={pc} output={po} | codex total: input={T['inputTokens']} cached={T['cachedInputTokens']} output={T['outputTokens']} reasoning={T['reasoningOutputTokens']}")
print("provider opencode billing (sum list_cost USD):", round(sum(p["usage"]["opencode"]["list_cost"] for p in prox), 6),
      "| billed cost field sum:", sum(p["usage"].get("cost", 0) for p in prox))
print("example provider usage:", json.dumps(prox[-1]["usage"]))
print("modelContextWindow reported:", tu[-1]["modelContextWindow"])
