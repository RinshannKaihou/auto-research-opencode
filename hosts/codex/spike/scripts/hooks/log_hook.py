#!/usr/bin/env python3
"""Generic hook: append stdin JSON (minus nothing secret) to logs/hooks.jsonl. argv[1] = mode."""
import json, sys, time, os
mode = sys.argv[1] if len(sys.argv) > 1 else "log"
data = json.load(sys.stdin)
keep = {k: data.get(k) for k in ("hook_event_name", "session_id", "turn_id", "tool_name", "tool_input", "prompt", "reason", "source", "trigger", "status", "token_usage", "model")}
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "hooks.jsonl"), "a") as f:
    f.write(json.dumps({"t": round(time.time(), 2), "mode": mode, "env_has_opencode": any(k.startswith("OPENCODE") for k in os.environ), **keep}) + "\n")
if mode == "deny_lookup":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                      "permissionDecisionReason": "lookup is blocked by spike policy hook"}}))
elif mode == "prompt_ctx":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "HOOK-CONTEXT: the magic number is 8128."}}))
elif mode == "stop":
    print("{}")
