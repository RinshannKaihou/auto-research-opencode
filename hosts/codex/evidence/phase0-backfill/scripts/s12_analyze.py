"""S12: summarize netns observer log after the calibration window."""
import json, collections, re
BF = "/workspace/codex-spike/backfill"
cal = json.load(open(f"{BF}/logs/s12_calibration.json"))["calibration_end_t"]
rows = [json.loads(l) for l in open(f"{BF}/logs/s12_netns.jsonl")]
cal_rows = [r for r in rows if r["t"] < cal and r.get("src") != "sniffer"]
run = [r for r in rows if r["t"] >= cal]
c = collections.Counter()
for r in run:
    if r["src"] == "dns": c[("dns", r.get("qname"))] += 1
    elif r["src"].startswith("sink"): c[(r["src"], r.get("host"))] += 1
    elif r["src"] == "sniff": c[("sniff-" + r["proto"], r["dst"])] += 1
print("calibration (positive control) rows:", len(cal_rows), sorted(set((r["src"], r.get("qname") or r.get("host") or r.get("dst")) for r in cal_rows)))
print("run window: t >=", cal, "rows:", len(run))
for k, v in sorted(c.items(), key=str): print(" ", k, v)
oa = re.compile(r"(openai\.com|chatgpt\.com|oaistatic|oaiusercontent)", re.I)
hits = [k for k in c if k[1] and oa.search(str(k[1]))]
other = [k for k in c if k[0].startswith("sniff") and k[1] != "127.0.0.1:18080"]
print("OpenAI/ChatGPT host hits in run window:", len(hits), hits)
print("connect attempts to anything other than the provider bridge 127.0.0.1:18080:", len(other), other)
