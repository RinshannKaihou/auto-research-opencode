"""S7: hooks via app-server: UserPromptSubmit (adds context), SessionStart, PreToolUse deny on MCP lookup,
PostModelRequest, SessionEnd (archive + process exit). Uses CODEX_HOME=home_hooks (trusted via hooks.state)."""
import os, json, sys, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appclient import *
a = AppServer("s7", env_extra={"CODEX_HOME": f"{ROOT}/home_hooks"}); a.init()
th = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
tid, d, notes = a.run_turn(th, "First call the fixture tool lookup with key alpha. Then, regardless of the result, reply: "
                           "LOOKUP=<the label, or BLOCKED plus the reason if the call was blocked> MAGIC=<the magic number from your context, or NONE>")
print("turn:", d["params"]["turn"]["status"]); print("final:", final_text(notes))
print("hook notes:", [(n["method"], n["params"].get("run", {}).get("eventName"), n["params"].get("run", {}).get("status")) for n in notes if n["method"].startswith("hook/")])
mcp = [n["params"]["item"] for n in notes if n.get("method") == "item/completed" and n["params"]["item"]["type"] == "mcpToolCall"]
print("mcp items:", [(m.get("tool"), m.get("status"), json.dumps(m.get("error") or m.get("result"))[:160]) for m in mcp])
s = len(a.notes)
print("archive:", a.request("thread/archive", {"threadId": th}, raise_err=False)); time.sleep(3)
print("post-archive hook notes:", [(n["method"], n["params"].get("run", {}).get("eventName")) for n in a.notes[s:] if n["method"].startswith("hook/")])
th2 = a.request("thread/start", {"cwd": f"{ROOT}/work"})["thread"]["id"]
a.run_turn(th2, "Reply OK"); a.close(); time.sleep(2)
print("--- hooks.jsonl ---")
for l in open(f"{ROOT}/logs/hooks.jsonl"):
    r = json.loads(l); print(r["hook_event_name"], r["mode"], (r.get("session_id") or "")[-6:], r.get("tool_name"), r.get("status"),
                            json.dumps(r.get("token_usage"))[:120] if r.get("token_usage") else "", "env_has_opencode=", r["env_has_opencode"])
