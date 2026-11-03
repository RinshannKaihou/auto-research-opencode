#!/usr/bin/env python3
"""S11 hook probe: logs only booleans/counts about env, never values."""
import json, sys, os, time
mode = sys.argv[1] if len(sys.argv) > 1 else "raw"
try: d = json.load(sys.stdin)
except Exception: d = {}
with open("/workspace/codex-spike/backfill/logs/hook_probe.jsonl", "a") as f:
    f.write(json.dumps({"t": round(time.time(), 2), "mode": mode, "event": d.get("hook_event_name"),
        "OPENCODE_API_KEY_present": "OPENCODE_API_KEY" in os.environ,
        "opencode_var_count": sum(k.startswith("OPENCODE") for k in os.environ)}) + "\n")
print("{}")
