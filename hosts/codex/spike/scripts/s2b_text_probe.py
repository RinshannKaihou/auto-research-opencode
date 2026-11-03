"""S2b: garbled-text probe: free-text answers (EN + ZH + after tool calls); check U+FFFD, control chars,
mojibake, and delta-concatenation == final item text."""
import os, json, sys, re, unicodedata; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s2b"); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
prompts = ["Explain in about 120 words how TCP congestion control works. Plain prose.",
           "用大约150个汉字解释什么是向量数据库。只用中文。",
           "Call fixture lookup with key alpha and fixture step with i=3, then write a 100-word summary of both results including the exact label and token.",
           "List 10 famous mathematicians with one-sentence descriptions each, as a numbered list.",
           "Write a short Python function that parses ISO-8601 dates, in a fenced code block, then one sentence explaining it."]
issues = 0
for p in prompts:
    _, d, notes = a.run_turn(th, p)
    ft = final_text(notes) or ""
    deltas = "".join(n["params"].get("delta", "") for n in notes if n.get("method") == "item/agentMessage/delta")
    bad = [c for c in ft if c == "\ufffd" or (unicodedata.category(c) == "Cc" and c not in "\n\t")]
    moj = re.findall(r"(Ã.|â€|å[\x80-\xbf])", ft)
    ok_delta = deltas.strip() == ft.strip() or ft.strip() in deltas
    issues += bool(bad or moj or not ok_delta or not ft)
    print(json.dumps({"status": d["params"]["turn"]["status"], "len": len(ft), "bad_chars": len(bad), "mojibake": len(moj), "delta_matches_final": ok_delta, "head": ft[:90]}, ensure_ascii=False))
print("turns with issues:", issues)
a.close()
