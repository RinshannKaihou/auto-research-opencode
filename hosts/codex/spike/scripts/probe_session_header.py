"""Direct probe (no Codex): which session header does OpenCode Go /responses accept? Prints status + error type only."""
import json, os, urllib.request, urllib.error
URL = "https://opencode.ai/zen/go/v1/responses"
body = json.dumps({"model": "deepseek-v4.1-flash", "input": "Reply OK", "max_output_tokens": 16}).encode()
for name, extra in (("none", {}), ("x-opencode-session", {"x-opencode-session": "probe-1"}), ("session-id", {"session-id": "probe-2"}),
                    ("thread-id", {"thread-id": "probe-3"}), ("x-codex-window-id", {"x-codex-window-id": "probe-4:0"})):
    req = urllib.request.Request(URL, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": "codex-spike-probe/0.1",
          "Authorization": "Bearer " + os.environ["OPENCODE_API_KEY"], **extra})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: print(name, r.status)
    except urllib.error.HTTPError as e: print(name, e.code, e.read()[:160].decode("utf8", "replace"))
