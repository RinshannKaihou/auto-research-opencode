"""S2: tool calling end-to-end (shell + local stdio MCP), ~20-step tool loop."""
import os, json, sys, re; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *

def write_s2_proxy_start():
    """Record proxy.jsonl's line count so s8_usage_compare.py can slice this run."""
    path = f"{ROOT}/logs/proxy.jsonl"
    n = sum(1 for _ in open(path)) if os.path.exists(path) else 0
    with open(f"{ROOT}/logs/s2.start", "w") as f:
        f.write(str(n))

def main():
    a = AppServer("s2"); a.init()
    th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
    prompt = ("Do these steps strictly sequentially, one tool call at a time:\n"
 "1. Run the shell command `env | grep -c OPENCODE` and remember its output.\n"
 "2. Call the fixture MCP tool `lookup` with key \"alpha\".\n"
 "3. Call the fixture MCP tool `step` with i=0. Then keep calling `step` with i set to the `next` value from the previous result, "
 "until you have called it with i=17 (18 step calls total).\n"
 "Finally reply with one line: ENV=<count> LABEL=<label from lookup> TOKENS=<comma-separated tokens from every step call in order>.")
    write_s2_proxy_start()
    tid, done, notes = a.run_turn(th, prompt, timeout=900)
    turn = done["params"]["turn"]
    items = [n["params"]["item"] for n in notes if n.get("method") == "item/completed"]
    types = {}
    for it in items: types[it["type"]] = types.get(it["type"], 0) + 1
    mcp = [it for it in items if it["type"] == "mcpToolCall"]
    cmd = [it for it in items if it["type"] == "commandExecution"]
    print("turn status:", turn.get("status"), turn.get("error"))
    print("item types:", types)
    print("events:", methods(notes))
    bad = [it for it in mcp if it.get("status") != "completed" or it.get("error")]
    print("mcp calls:", len(mcp), "failed:", len(bad), [ (b.get("tool"), b.get("error")) for b in bad][:5])
    print("mcp args seq:", [ (m.get("tool"), m.get("arguments")) for m in mcp])
    for c in cmd: print("cmd:", c.get("command"), "->", repr((c.get("aggregatedOutput") or "")[:80]), "exit", c.get("exitCode"))
    ft = final_text(notes); print("final:", ft)
    exp = ",".join(f"T{i*7%97:02d}" for i in range(18))
    m = re.search(r"TOKENS=([A-Z0-9, ]+)", ft or "")
    print("expected tokens:", exp); print("tokens match:", bool(m) and m.group(1).replace(" ", "") == exp)
    print("non-ascii chars in final:", [c for c in (ft or "") if ord(c) > 127][:20])
    json.dump({"thread": th, "turn": tid}, open(f"{ROOT}/logs/s2_ids.json", "w"))
    a.close()

if __name__ == "__main__":
    main()
