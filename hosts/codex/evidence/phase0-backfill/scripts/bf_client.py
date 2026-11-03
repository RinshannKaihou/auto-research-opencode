"""Backfill client: reuses spike appclient.AppServer but with backfill CODEX_HOME/logdir/cwd.
Counts raw occurrences of the key in every server message BEFORE redaction (count only)."""
import json, os, subprocess, threading, itertools, sys
sys.path.insert(0, "/workspace/codex-spike/scripts")
import appclient
from appclient import final_text, methods
BF = "/workspace/codex-spike/backfill"
KEY = os.environ.get("OPENCODE_API_KEY") or "\x00never"
RAW_KEY_HITS = {"stdout_msgs": 0}
class BFServer(appclient.AppServer):
    def __init__(self, log_name, home=f"{BF}/home", cwd=f"{BF}/work"):
        env = dict(os.environ, CODEX_HOME=home, RUST_LOG="warn")
        self.logf = open(f"{BF}/logs/{log_name}.jsonl", "a"); self.errf = open(f"{BF}/logs/{log_name}.stderr.raw", "a")
        self.p = subprocess.Popen([appclient.CODEX, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.errf, env=env, cwd=cwd, text=True, bufsize=1)
        self.ids = itertools.count(1); self.pending = {}; self.notes = []; self.cv = threading.Condition()
        self.server_request_handler = None
        threading.Thread(target=self._reader, daemon=True).start()
    def _log(self, d, o):
        s = json.dumps({"dir": d, **o})
        if d == "<": RAW_KEY_HITS["stdout_msgs"] += s.count(KEY)
        self.logf.write(appclient.redact(s) + "\n"); self.logf.flush()
