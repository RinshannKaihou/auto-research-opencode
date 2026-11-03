"""Backfill S2/S3(part1)/S4 + S11 hook probes, all inside netns cxbf (S12). Phase 1 ends with SIGKILL of app-server."""
import json, sys, time, uuid, signal
sys.path.insert(0, "/workspace/codex-spike/backfill/scripts")
from bf_client import *
P = f"{BF}/logs/proxy.jsonl"
OUT = open(f"{BF}/logs/bf_main.summary.txt", "a")
def out(*a):
    s = " ".join(str(x) for x in a); print(s); OUT.write(s + "\n"); OUT.flush()
def nlines(): 
    try: return len(open(P).readlines())
    except FileNotFoundError: return 0
def proxy_since(n): return [json.loads(l) for l in open(P).readlines()[n:]]
def items(notes, typ=None):
    return [n["params"]["item"] for n in notes if n.get("method") == "item/completed" and (typ is None or n["params"]["item"].get("type") == typ)]
def summarize(name, th, prompt, **kw):
    n0 = nlines()
    tid, done, notes = a.run_turn(th, prompt, timeout=240, **kw)
    reqs = proxy_since(n0)
    tools = sorted(set(t for r in reqs for t in (r.get("tool_names") or [])))
    out(f"\n## {name} thread={th} turn={tid} status={done['params']['turn']['status']} model_calls={len(reqs)} http={[r.get('status') for r in reqs]}")
    out("  tools sent to model:", tools)
    out("  item types:", methods([{"method": i["type"]} for i in items(notes)]))
    for i in items(notes):
        if i["type"] == "commandExecution": out("  cmd:", json.dumps(i.get("command"))[:160], "exit=", i.get("exitCode"), "status=", i.get("status"), "out=", json.dumps((i.get("aggregatedOutput") or "")[:160]))
        if i["type"] == "mcpToolCall": out("  mcp:", i.get("server"), i.get("tool"), i.get("status"), json.dumps(i.get("result"))[:200])
        if i["type"] in ("webSearch", "collabAgentToolCall", "fileChange"): out("  ", i["type"], json.dumps(i)[:300])
    out("  final:", json.dumps(final_text(notes))[:500])
    if done["params"]["turn"].get("error"): out("  error:", json.dumps(done["params"]["turn"]["error"])[:400])
    return tid, notes, reqs
a = BFServer("bf_main_p1"); init = a.init()
out("=== run", time.strftime("%F %T %z"), "userAgent:", init.get("userAgent"))
CW = f"{BF}/work/s2"
# ---- S2 ----
A = a.request("thread/start", {"cwd": CW, "config": {
    "features.shell_tool": False, "features.unified_exec": False, "features.multi_agent": False, "web_search": "disabled",
    "mcp_servers.tokstub.env": {"STUB_LOG": f"{BF}/logs/tokstub.jsonl", "RESEARCH_TOKEN": "test-token-thread-A"}}})["thread"]["id"]
B = a.request("thread/start", {"cwd": CW, "config": {
    "features.multi_agent": True, "web_search": "disabled",
    "mcp_servers.tokstub.env": {"STUB_LOG": f"{BF}/logs/tokstub.jsonl", "RESEARCH_TOKEN": "test-token-thread-B"}}})["thread"]["id"]
W = a.request("thread/start", {"cwd": CW, "config": {"web_search": "live", "features.multi_agent": False}})["thread"]["id"]
out("S2 threads: A(coordinator-like)=", A, "B(node-like)=", B, "W(web on)=", W)
summarize("S2-A shell/subagent/web OFF, token A", A,
  "Tool availability test. Do each step in order, do not skip and do not guess: (1) run the shell command `echo SHELL_RAN` with a shell/exec tool; "
  "(2) spawn a sub-agent and ask it to say hi; (3) do a web search for 'opencode go'; (4) call the tokstub MCP tool `whoami`. "
  "Then reply with exactly 4 lines: `STEP n: DONE <short result>` or `STEP n: UNAVAILABLE`.")
summarize("S2-B shell/subagent ON, web OFF, token B", B,
  "Do in order: (1) run the shell command `env` and then `env | grep -c OPENCODE` (two separate commands); (2) call the tokstub MCP tool `whoami`; "
  "(3) do NOT spawn any agent, just state whether a tool named spawn_agent is available to you. Reply with 3 short lines.")
summarize("S2-W web_search live", W,
  "Use your web search tool to search for 'OpenCode Go zen' and give the title of one result. If you have no web search tool, reply exactly NO_WEB_TOOL.")
# ---- S4 ----
D = a.request("thread/start", {"cwd": f"{BF}/work/s4", "approvalPolicy": "never", "config": {"default_permissions": "ro_env"}})["thread"]["id"]
summarize("S4-D default_permissions=ro_env, approval never", D,
  "Using the shell tool, run each of these as a SEPARATE command and report each exit code and first output line verbatim: "
  "(1) `cat .env`  (2) `echo x > shell_write.txt`  (3) `apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: patched.txt\n+hi\n*** End Patch\nEOF`  (4) `cat readme.txt`. "
  "Do not retry with other methods.")
import os
out("S4 fs after: shell_write.txt exists=", os.path.exists(f"{BF}/work/s4/shell_write.txt"), "patched.txt exists=", os.path.exists(f"{BF}/work/s4/patched.txt"))
# ---- S3 part 1 ----
C = a.request("thread/start", {"cwd": f"{BF}/work/s3"})["thread"]["id"]
CUMID = "bf-s3-" + uuid.uuid4().hex[:12]
summarize("S3-C turn with clientUserMessageId", C, "Reply with exactly: S3-ACK", clientUserMessageId=CUMID)
pre = a.request("thread/read", {"threadId": C, "includeTurns": True})["thread"]
pre_ids = [it.get("clientId") for t in pre.get("turns", []) for it in t.get("items", []) if it.get("type") == "userMessage"]
out("S3 before kill: clientIds in thread/read:", pre_ids)
json.dump({"thread": C, "cumid": CUMID}, open(f"{BF}/run/s3_state.json", "w"))
out("raw key hits in app-server stdout msgs (phase1):", RAW_KEY_HITS["stdout_msgs"])
pid = a.p.pid; os.kill(pid, signal.SIGKILL); a.p.wait()
out("S3: app-server pid", pid, "SIGKILLed, returncode", a.p.returncode)
